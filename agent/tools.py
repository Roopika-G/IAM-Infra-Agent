"""The agent's connection to the world: the MCP servers (the LLM gets read tools
only; code can call any tool) and the intake snapshot built from them.
"""

import json
import sys
import time
from contextlib import AsyncExitStack, nullcontext
from typing import Any

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.models import DriftRow, Evidence, Incident, PodRow, Settings, Snapshot
from agent.records import tool_span


# ---- MCP toolbox -----------------------------------------------------------

SERVER_SCRIPTS = {
    "knowledge": "mcp_servers/knowledge/server.py",
    "kubernetes": "mcp_servers/kubernetes/server.py",
    "pf_admin": "mcp_servers/pf_admin/server.py",
    "repo_read": "mcp_servers/repo_read/server.py",
    "helm_ops": "mcp_servers/helm_ops/server.py",
    "repo_config": "mcp_servers/repo_config/server.py",
    "github_pr": "mcp_servers/github_pr/server.py",
}

# The only tools the LLM may call. Everything else is for code nodes.
READ_TOOLS = {
    "search_vector", "get_baseline_value", "get_logs",
    "get_pod_status", "get_events", "get_live_config_value", "get_pod_logs", "get_pod_spec",
    "get_workload_status", "get_secret_keys",
    "get_pf_datastore", "get_pf_cluster_status", "get_pf_version", "get_pf_license", "get_pf_certificates",
    "read_values_yaml_key", "read_server_profile_file", "diff_vs_golden", "find_in_repo",
    "get_helm_release",
}


class ToolFailed(Exception):
    """An MCP tool returned an error. The message is the tool's own reason."""


def parse_result(result) -> Any:
    """MCP result -> Python value. Lists arrive in structured_content as
    {"result": [...]}; dicts arrive as one JSON text block."""
    if result.is_error:
        raise ToolFailed(" ".join(getattr(c, "text", "") for c in result.content) or "tool failed")
    structured = getattr(result, "structured_content", None)
    if structured and "result" in structured:
        return structured["result"]
    texts = [c.text for c in result.content if getattr(c, "text", None) is not None]
    if not texts:
        return None
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    return [json.loads(t) for t in texts]


class Toolbox:
    def __init__(self, settings: Settings, servers: dict[str, str] | None = None):
        self.settings = settings
        self.servers = servers or SERVER_SCRIPTS
        self._stack = AsyncExitStack()
        self._session_of: dict[str, ClientSession] = {}
        self._tools: dict[str, Any] = {}      # name -> mcp Tool (has description + input_schema)
        self.on_call = None                   # optional hook(name, args, result, error, duration_ms): the run log

    async def __aenter__(self) -> "Toolbox":
        for name, script in self.servers.items():
            path = script if script.startswith("/") else str(self.settings.repo_root / script)
            params = StdioServerParameters(command=sys.executable, args=[path], env=self.settings.mcp_env)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            for tool in (await session.list_tools()).tools:
                self._session_of[tool.name] = session
                self._tools[tool.name] = tool
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def call(self, name: str, args: dict | None = None, timeout: float | None = None, traced: bool = True) -> Any:
        """Call any tool by name and return the parsed result. Raises ToolFailed. timeout is in seconds.

        Every call, from the LLM or from code, is reported to on_call."""
        args = args or {}
        start = time.monotonic()
        # Tools the LLM picks already get a Phoenix span from LangChain; only code-run calls need our own.
        with (tool_span(name, args) if traced else nullcontext(lambda result, error: None)) as record:
            try:
                session = self._session_of.get(name)
                if session is None:
                    raise ToolFailed(f"no such tool: {name}")
                value = parse_result(await session.call_tool(name, args, read_timeout_seconds=timeout))
            except ToolFailed as e:
                record(None, str(e))
                self._report(name, args, None, str(e), start)
                raise
            record(value, None)
        self._report(name, args, value, None, start)
        return value

    def _report(self, name, args, result, error, start) -> None:
        if self.on_call:
            self.on_call(name, args, result, error, int((time.monotonic() - start) * 1000))

    def read_tools(self) -> list[StructuredTool]:
        """The READ_TOOLS that exist, as LangChain tools for the LLM. Errors
        come back as text ("ERROR: <reason>") so the model can read them."""
        limit = self.settings.tool_result_chars
        tools = []
        for name in sorted(READ_TOOLS & self._tools.keys()):
            async def run(_name=name, **kwargs) -> str:
                try:
                    value = await self.call(_name, kwargs, traced=False)
                except ToolFailed as e:
                    return f"ERROR: {e}"
                text = value if isinstance(value, str) else json.dumps(value, default=str)
                return text[:limit] + (" …[truncated]" if len(text) > limit else "")
            tools.append(StructuredTool(
                name=name, description=self._tools[name].description or name,
                args_schema=self._tools[name].input_schema, coroutine=run,
            ))
        return tools


# ---- intake: the snapshot taken before the LLM starts ----------------------

def component_of(pod_name: str) -> str | None:
    if "admin" in pod_name:
        return "admin"
    if "engine" in pod_name:
        return "engine"
    if pod_name.startswith("postgres"):
        return "postgres"
    return None


def drift_state(golden: str, live: str | None) -> str:
    return "unknown" if live is None else ("ok" if live == golden else "drift")


async def _try(tools, name: str, args: dict, unavailable: list[str]):
    try:
        return await tools.call(name, args, traced=False)     # intake shows as one Phoenix row; the detail is in the Postgres record
    except ToolFailed as e:
        unavailable.append(f"{name} ({str(e)[:80]})")
        return None


async def scan_drift(tools, unavailable: list[str]) -> tuple[list[PodRow], list[DriftRow]]:
    """The pods, and every baseline key against what its running container actually sees."""
    pod_rows, by_component = [], {}
    for p in await _try(tools, "get_pod_status", {}, unavailable) or []:
        pod_rows.append(PodRow(name=p["pod"], phase=p["phase"], ready=p["ready"], restarts=sum(c["restart_count"] for c in p["containers"])))
        if component_of(p["pod"]):
            by_component[component_of(p["pod"])] = p["pod"]
    drift_rows = []
    for row in await _try(tools, "list_baseline", {}, unavailable) or []:
        product, _, env_name = row["key"].partition(".envs.")
        pod = by_component.get(product.removeprefix("pingfederate-"))
        live = None
        if pod and env_name:
            got = await _try(tools, "get_live_config_value", {"pod": pod, "key": env_name}, unavailable)
            live = got["value"] if got else None
        drift_rows.append(DriftRow(key=row["key"], golden=row["golden_value"], live=live, state=drift_state(row["golden_value"], live)))
    return pod_rows, drift_rows


async def build_snapshot(tools, incident: Incident, evidence_chars: int = 400) -> tuple[Snapshot, list[Evidence]]:
    snap = Snapshot()
    evidence: list[Evidence] = []

    def note(tool: str, args: dict, summary: str) -> None:
        evidence.append(Evidence(id=f"e{len(evidence) + 1}", tool=tool, args=args, summary=summary[:evidence_chars]))

    # pods, and the drift scan (every baseline key against what the running container sees)
    snap.pods, snap.drift = await scan_drift(tools, snap.unavailable)
    note("get_pod_status", {}, "; ".join(f"{p.name} {p.ready} {p.phase}" for p in snap.pods) or "no pods listed")
    drifted = [d for d in snap.drift if d.state == "drift"]
    note("drift scan (list_baseline + get_live_config_value)", {},
         f"{len(drifted)} of {len(snap.drift)} keys drifted" + "".join(f"; {d.key}: golden={d.golden} live={d.live}" for d in drifted))

    # license and certificates
    lic = await _try(tools, "get_pf_license", {}, snap.unavailable)
    certs = await _try(tools, "get_pf_certificates", {}, snap.unavailable)
    if lic:
        snap.license_days = lic.get("days_until_expiry")
    if certs:
        days = [c["days_until_expiry"] for cat in certs.values() if isinstance(cat, list) for c in cat if c.get("days_until_expiry") is not None]
        snap.cert_days = min(days) if days else None
    note("get_pf_license / get_pf_certificates", {}, f"license {snap.license_days} days; soonest certificate {snap.cert_days} days")

    # PF's own view of the cluster
    cluster = await _try(tools, "get_pf_cluster_status", {}, snap.unavailable)
    if cluster:
        states = {n["replicationStatus"] for n in cluster["nodes"]}
        snap.replication = "SUCCEEDED" if states == {"SUCCEEDED"} and not cluster.get("replicationRequired") else ",".join(sorted(states))
    note("get_pf_cluster_status", {}, f"replication {snap.replication}")

    # how noisy are the logs right now
    logs = await _try(tools, "get_logs", {"level": "ERROR", "limit": 50}, snap.unavailable)
    snap.recent_errors = len(logs) if logs is not None else None
    note("get_logs", {"level": "ERROR", "limit": 50}, f"{snap.recent_errors} recent ERROR lines")

    return snap, evidence


def format_evidence(evidence: list[Evidence]) -> str:
    return "\n".join(f"{e.id}  {e.tool}: {e.summary}" for e in evidence) or "(none yet)"
