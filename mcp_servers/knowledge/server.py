"""knowledge MCP server: read-only Postgres access for the diagnosis agent.

Three separate tools, one server -- all three share the same backend and
the same read-only DB credential (AGENT_DB_DSN), so splitting them into
separate servers would add processes without adding any security boundary.
Each tool answers a different question, which is why they stay separate
tools (see plan.md Phase 6).

  search_vector        -- "what does the architecture doc say about this?"
  get_baseline_value   -- "what is the known-good value for this exact key?"
  get_logs             -- "what else was logged around this error?"

Run (stdio transport, how an MCP client launches it):
    uv run python mcp_servers/knowledge/server.py
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "search"))

import baseline  # noqa: E402  (search/baseline.py)
import query  # noqa: E402  (search/query.py)

DB_DSN = os.environ.get("AGENT_DB_DSN", "postgresql://postgres@localhost:5432/postgres")

MAX_LOG_ROWS = 100
MAX_MESSAGE_CHARS = 500

mcp = MCPServer("knowledge")


@mcp.tool()
def search_vector(query_text: str, top_k: int = 3) -> list[dict]:
    """Hybrid keyword + vector search over the golden-architecture knowledge
    base. Input is symptom text (e.g. an error message). Returns up to top_k
    chunks as {title, content, related_keys, score}; related_keys are the
    exact config_baseline keys each chunk is about -- pass those to
    get_baseline_value next."""
    top_k = max(1, min(top_k, 10))
    return query.search(query_text, top_k=top_k)


@mcp.tool()
def get_baseline_value(key: str) -> dict:
    """Exact lookup of the frozen known-good value for one config key, e.g.
    'pingfederate-admin.envs.POSTGRES_JDBC_URL'. Returns {key, golden_value,
    source_file}. source_file is the file the key lives in (the file to
    patch), and for values.yaml keys the key itself is the exact dotted path
    to pass to repo_read / repo_config. golden_value and source_file are null
    if the key isn't in the baseline."""
    entry = baseline.get_baseline_entry(key)
    return {
        "key": key,
        "golden_value": entry["golden_value"] if entry else None,
        "source_file": entry["source_file"] if entry else None,
    }


@mcp.tool()
def get_logs(
    since: str | None = None,
    until: str | None = None,
    level: str | None = None,
    contains: str | None = None,
    pf_role: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read PingFederate log lines from public.pf_logs_raw, newest first.
    since/until are ISO-8601 timestamps (e.g. '2026-09-23T18:00:00').
    level is one of ERROR/WARN/INFO/DEBUG. contains is a case-insensitive
    substring match on the message. pf_role is 'admin' or 'engine'.
    Returns at most 100 rows, messages truncated to 500 chars."""
    limit = max(1, min(limit, MAX_LOG_ROWS))

    clauses, params = [], []
    if since:
        clauses.append("time >= %s")
        params.append(datetime.fromisoformat(since))
    if until:
        clauses.append("time <= %s")
        params.append(datetime.fromisoformat(until))
    if level:
        clauses.append("data->>'level' = %s")
        params.append(level.upper())
    if contains:
        clauses.append("data->>'message' ILIKE %s")
        params.append(f"%{contains}%")
    if pf_role:
        clauses.append("data->>'pf_role' = %s")
        params.append(pf_role)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT time, data->>'level', data->>'logger', data->>'pf_role',
               data->>'pod_name', data->>'message'
        FROM public.pf_logs_raw
        {where}
        ORDER BY time DESC
        LIMIT %s
    """
    params.append(limit)

    with psycopg.connect(DB_DSN) as conn:
        rows = conn.execute(sql, params).fetchall()

    return [
        {
            "time": ts.isoformat(),
            "level": lvl,
            "logger": logger,
            "pf_role": role,
            "pod_name": pod,
            "message": (msg or "")[:MAX_MESSAGE_CHARS],
        }
        for ts, lvl, logger, role, pod, msg in rows
    ]


if __name__ == "__main__":
    mcp.run()
