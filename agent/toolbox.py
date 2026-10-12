"""The agent's connection to the MCP servers.

Each server is started once as a subprocess and kept open for the whole run.
Two ways to use a tool:

  * the LLM gets READ_TOOLS only, as LangChain tools (read_tools()).
  * code nodes call any tool, including the write ones, with call().

The LLM is never handed a write tool. That is the read/write boundary.
"""

import json
import sys
import time
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.settings import Settings
from agent.tracing import tool_span

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

    async def call(self, name: str, args: dict | None = None, timeout: float | None = None) -> Any:
        """Call any tool by name and return the parsed result. Raises ToolFailed. timeout is in seconds.

        Every call, from the LLM or from code, is reported to on_call."""
        args = args or {}
        start = time.monotonic()
        with tool_span(name, args) as record:
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
                    value = await self.call(_name, kwargs)
                except ToolFailed as e:
                    return f"ERROR: {e}"
                text = value if isinstance(value, str) else json.dumps(value, default=str)
                return text[:limit] + (" …[truncated]" if len(text) > limit else "")
            tools.append(StructuredTool(
                name=name, description=self._tools[name].description or name,
                args_schema=self._tools[name].input_schema, coroutine=run,
            ))
        return tools
