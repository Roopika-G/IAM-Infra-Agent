"""Data layer for the UI: reads the same sources the agent's tools do, by
calling the MCP servers' plain functions directly (kubernetes, pf_admin) and
querying Postgres. Read-only; the only side effect anywhere in ui/ is the
Simulations page running Error_Simulation/sim_a_jdbc_url.sh on request.

Everything slow (k8s exec, PF API, gh) is cached for a few seconds so HTMX
polling doesn't hammer the cluster.
"""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_servers import common  # noqa: E402
from mcp_servers.kubernetes import server as kube  # noqa: E402
from mcp_servers.pf_admin import server as pfadmin  # noqa: E402

DB_DSN = os.environ.get("AGENT_DB_DSN", "postgresql://postgres@localhost:5432/postgres")
CLOSED = ("RESOLVED", "FAILED")

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def cached(key: str, ttl: float, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


# --- incidents ---------------------------------------------------------

_INCIDENT_COLS = "id, status, pf_role, log_type, logger, exception_type, sample_message, first_seen, last_seen, occurrence_count, fingerprint"


def _incident(row) -> dict:
    keys = [c.strip() for c in _INCIDENT_COLS.split(",")]
    return dict(zip(keys, row))


def list_incidents(only_open: bool = True, limit: int = 200) -> list[dict]:
    where = "WHERE status NOT IN ('RESOLVED','FAILED')" if only_open else ""
    with psycopg.connect(DB_DSN) as conn:
        rows = conn.execute(
            f"SELECT {_INCIDENT_COLS} FROM agent.incidents {where} ORDER BY last_seen DESC LIMIT %s", (limit,)
        ).fetchall()
    return [_incident(r) for r in rows]


def get_incident(incident_id: int) -> dict | None:
    with psycopg.connect(DB_DSN) as conn:
        row = conn.execute(f"SELECT {_INCIDENT_COLS} FROM agent.incidents WHERE id = %s", (incident_id,)).fetchone()
        if not row:
            return None
        incident = _incident(row)
        logs = conn.execute(
            """SELECT time, data->>'pf_role', data->>'pod_name', data->>'message'
               FROM public.pf_logs_raw
               WHERE data->>'message' = %s
               ORDER BY time DESC LIMIT 10""",
            (incident["sample_message"],),
        ).fetchall()
    # pf_logs_raw.time is a timestamp WITHOUT time zone, stored in UTC; tag it
    # so the browser doesn't read it as local time.
    incident["recent_logs"] = [
        {"time": t.replace(tzinfo=timezone.utc), "role": r, "pod": p, "message": m} for t, r, p, m in logs
    ]
    return incident


def open_counts_by_role() -> dict[str, int]:
    with psycopg.connect(DB_DSN) as conn:
        rows = conn.execute(
            "SELECT pf_role, count(*) FROM agent.incidents WHERE status NOT IN ('RESOLVED','FAILED') GROUP BY 1"
        ).fetchall()
    return {role: n for role, n in rows}


# --- fleet -------------------------------------------------------------

def _component_of(pod_name: str) -> str | None:
    if "admin" in pod_name:
        return "admin"
    if "engine" in pod_name:
        return "engine"
    if pod_name.startswith("postgres"):
        return "postgres"
    return None


def _baseline() -> list[tuple[str, str]]:
    with psycopg.connect(DB_DSN) as conn:
        return conn.execute("SELECT key, golden_value FROM agent.config_baseline ORDER BY key").fetchall()


def _drift(pods_by_component: dict[str, str]) -> list[dict]:
    """Compare every baseline key's golden value with what the pod's
    container actually sees (same primitive as get_live_config_value)."""
    jobs = []
    for key, golden in _baseline():
        product, _, rest = key.partition(".envs.")
        component = product.removeprefix("pingfederate-")
        pod = pods_by_component.get(component)
        jobs.append((key, golden, component, pod, rest))

    def check(job):
        key, golden, component, pod, env_name = job
        if not pod:
            return {"key": key, "component": component, "golden": golden, "live": None, "state": "unknown"}
        try:
            live = kube.get_live_config_value(pod, env_name)["value"]
        except Exception:
            return {"key": key, "component": component, "golden": golden, "live": None, "state": "unknown"}
        return {"key": key, "component": component, "golden": golden, "live": live,
                "state": "ok" if live == golden else "drift"}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(check, jobs))
    order = {"drift": 0, "unknown": 1, "ok": 2}
    return sorted(results, key=lambda r: (order[r["state"]], r["key"]))


def _fleet() -> dict:
    out = {"error": None, "components": [], "drift": [], "pf": None, "pf_error": None, "version": None}
    try:
        pods = kube.get_pod_status()
    except Exception as e:
        out["error"] = f"Kubernetes API unreachable: {e}"
        return out

    by_component = {}
    for p in pods:
        c = _component_of(p["pod"])
        if c:
            by_component[c] = p

    try:
        cluster = pfadmin.get_pf_cluster_status()
        out["pf"] = {n["mode"]: n for n in cluster["nodes"]}
        out["version"] = cluster["nodes"][0].get("version") if cluster["nodes"] else None
    except Exception as e:
        out["pf_error"] = str(e)[:120]

    counts = open_counts_by_role()
    out["drift"] = _drift({c: p["pod"] for c, p in by_component.items()})

    drift_n = {}
    for d in out["drift"]:
        if d["state"] == "drift":
            drift_n[d["component"]] = drift_n.get(d["component"], 0) + 1

    mode_of = {"admin": "CLUSTERED_CONSOLE", "engine": "CLUSTERED_ENGINE"}
    for name in ("admin", "engine", "postgres"):
        p = by_component.get(name)
        if not p:
            out["components"].append({"name": name, "present": False})
            continue
        restarts = sum(c["restart_count"] for c in p["containers"])
        node = (out["pf"] or {}).get(mode_of.get(name, ""))
        ready_all = all(c["ready"] for c in p["containers"])
        out["components"].append({
            "name": name, "present": True, "pod": p["pod"], "phase": p["phase"], "ready": p["ready"],
            "restarts": restarts,
            "replication": node["replicationStatus"] if node else None,
            "drift": drift_n.get(name, 0),
            "incidents": counts.get(name, 0),
            "healthy": p["phase"] == "Running" and ready_all,
        })
    return out


def fleet() -> dict:
    return cached("fleet", 8, _fleet)


# --- pull requests -----------------------------------------------------

def _remediation_prs() -> dict:
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--limit", "50",
             "--json", "number,title,headRefName,state,createdAt,url,author"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip()[:200], "prs": []}
        prs = [p for p in json.loads(result.stdout) if p["headRefName"].startswith("remediation/inc-")]
        return {"error": None, "prs": prs}
    except Exception as e:
        return {"error": str(e)[:200], "prs": []}


def remediation_prs() -> dict:
    return cached("prs", 20, _remediation_prs)


# --- simulations -------------------------------------------------------

SIM_A_SCRIPT = ROOT / "Error_Simulation" / "sim_a_jdbc_url.sh"
SIM_A_GOLDEN = "jdbc:postgresql://postgres.pingfederate.svc.cluster.local:5432/postgres"
SIM_LOG = Path("/tmp/sim_a_ui.log")
_sim = {"proc": None, "mode": None, "started": None}


def sim_a_state() -> str:
    """'healthy' / 'corrupted' / 'unknown', judged from the working-tree values.yaml."""
    try:
        text = (ROOT / common.VALUES_FILE).read_text()
        url = common.find_scalar(text, "pingfederate-admin.envs.POSTGRES_JDBC_URL").value
    except Exception:
        return "unknown"
    return "healthy" if url == SIM_A_GOLDEN else "corrupted"


def sim_a_run(mode: str) -> str | None:
    """Start the script in the background. Returns an error string, or None."""
    if mode not in ("inject", "restore"):
        return "unknown mode"
    proc = _sim["proc"]
    if proc and proc.poll() is None:
        return f"Sim A {_sim['mode']} is still running"
    state = sim_a_state()
    if mode == "inject" and state == "corrupted":
        return "Already corrupted; restore it first"
    if mode == "restore" and state == "healthy":
        return "Already healthy; nothing to restore"
    log = open(SIM_LOG, "w")
    _sim.update(proc=subprocess.Popen([str(SIM_A_SCRIPT), mode], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT),
                mode=mode, started=time.time())
    cache_clear()
    return None


def sim_a_status() -> dict:
    proc = _sim["proc"]
    running = bool(proc and proc.poll() is None)
    tail = ""
    if SIM_LOG.exists():
        tail = "\n".join(SIM_LOG.read_text().splitlines()[-12:])
    return {"running": running, "mode": _sim["mode"], "exit_code": None if running or not proc else proc.returncode,
            "tail": tail, "state": sim_a_state()}


def cache_clear():
    with _lock:
        _cache.clear()
