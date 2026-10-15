"""Everything that reads or writes Postgres, plus optional Phoenix tracing:
incidents and their status, earlier attempts (for a retry), the pull-request
status sync, and the run log (one row per agent run, one per step).
"""

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from agent.models import Incident, render


# ---- incidents -------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # keys live in the repo-root .env (never overrides your shell)

DB_DSN = os.environ.get("AGENT_DB_DSN", "postgresql://postgres@localhost:5432/postgres")
COLUMNS = "id, status, pf_role, logger, sample_message, first_seen, last_seen, occurrence_count"


def load_incident(incident_id: int) -> Incident:
    with psycopg.connect(DB_DSN) as conn:
        row = conn.execute(f"SELECT {COLUMNS} FROM agent.incidents WHERE id = %s", (incident_id,)).fetchone()
    if row is None:
        raise LookupError(f"no incident #{incident_id}")
    return Incident(**dict(zip(COLUMNS.replace(" ", "").split(","), row)))


def set_status(incident_id: int, status: str) -> None:
    with psycopg.connect(DB_DSN) as conn:
        conn.execute("UPDATE agent.incidents SET status = %s WHERE id = %s", (status, incident_id))


def claim_merged() -> int | None:
    """Atomically take one MERGED incident to deploy (it becomes DEPLOYING)."""
    with psycopg.connect(DB_DSN) as conn:
        row = conn.execute(
            """UPDATE agent.incidents SET status = 'DEPLOYING' WHERE id = (
                   SELECT id FROM agent.incidents WHERE status = 'MERGED' ORDER BY last_seen LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING id""").fetchone()
    return row[0] if row else None


def latest_run(incident_id: int) -> tuple[int, dict | None] | None:
    """(run id, outcome) of the agent's most recent run on the incident."""
    with psycopg.connect(DB_DSN) as conn:
        row = conn.execute("SELECT id, outcome FROM agent.runs WHERE incident_id = %s ORDER BY started_at DESC LIMIT 1", (incident_id,)).fetchone()
    return (row[0], row[1]) if row else None


def claim_new(matches, max_age_minutes: int = 15) -> Incident | None:
    """Atomically take the oldest NEW (or RETRY) incident for which matches(incident) is true.

    Only incidents seen in the last max_age_minutes are eligible, so old
    incidents from earlier runs are never picked up by a freshly started worker.
    """
    with psycopg.connect(DB_DSN) as conn:
        rows = conn.execute(
            f"SELECT {COLUMNS} FROM agent.incidents WHERE (status = 'NEW' AND last_seen > now() - make_interval(mins => %s)) "
            "OR status = 'RETRY' ORDER BY first_seen",          # RETRY: a deploy failed and the agent has attempts left
            (max_age_minutes,),
        ).fetchall()
        for row in rows:
            incident = Incident(**dict(zip(COLUMNS.replace(" ", "").split(","), row)))
            if matches(incident):
                taken = conn.execute("UPDATE agent.incidents SET status = 'DIAGNOSING' WHERE id = %s AND status IN ('NEW', 'RETRY') RETURNING id", (incident.id,)).fetchone()
                if taken:
                    return incident
    return None


# ---- earlier attempts ------------------------------------------------------

MAX_ATTEMPTS = 2     # the agent gets this many tries; after that a human is asked


def attempt_count(incident_id: int) -> int:
    with psycopg.connect(DB_DSN) as conn:
        return conn.execute("SELECT count(*) FROM agent.runs WHERE incident_id = %s", (incident_id,)).fetchone()[0]


def render_notes(attempts: list[dict]) -> list[str]:
    """attempts: [{n, changes: [(key, value)], pr_url, deploy_status, deploy_detail}], oldest first."""
    notes = []
    for a in attempts:
        if not a.get("deploy_status"):
            continue                                   # no known result: nothing to learn from
        changed = ", ".join(f"{k} -> {v}" for k, v in a["changes"]) or "no file change"
        notes.append(render(
            "retry_context", n=a["n"],
            summary=f"changed {changed}" + (f" (pull request {a['pr_url']})" if a.get("pr_url") else ""),
            result=f"{a['deploy_status']}: {a['deploy_detail']}"))
    return notes


def retry_notes(incident_id: int) -> list[str]:
    attempts = []
    with psycopg.connect(DB_DSN) as conn:
        runs = conn.execute("SELECT id, outcome FROM agent.runs WHERE incident_id = %s ORDER BY started_at", (incident_id,)).fetchall()
        for n, (run_id, outcome) in enumerate(runs, 1):
            events = {k: d for k, d in conn.execute(
                "SELECT kind, data FROM agent.run_events WHERE run_id = %s AND kind IN ('decision', 'deploy_result') ORDER BY seq", (run_id,)).fetchall()}
            decision, deploy = events.get("decision") or {}, events.get("deploy_result") or {}
            attempts.append({"n": n, "changes": [(c["key_path"], c["new_value"]) for c in decision.get("changes", [])],
                             "pr_url": (outcome or {}).get("url"), "deploy_status": deploy.get("status"), "deploy_detail": deploy.get("detail")})
    return render_notes(attempts)


# ---- pull request status ---------------------------------------------------

STATUS_FOR_PR_STATE = {"MERGED": "MERGED", "CLOSED": "PR_REJECTED"}


def pr_state(url: str) -> str | None:
    """OPEN, MERGED or CLOSED as GitHub reports it, or None if gh could not say."""
    try:
        out = subprocess.run(["gh", "pr", "view", url, "--json", "state", "-q", ".state"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def sync_pr_status(get_state=pr_state) -> list[tuple[int, str]]:
    """Update every incident waiting on a PR. Returns [(incident_id, new_status)]."""
    with psycopg.connect(DB_DSN) as conn:
        waiting = conn.execute(
            """SELECT i.id, lr.outcome->>'url' FROM agent.incidents i
               JOIN LATERAL (SELECT outcome FROM agent.runs r WHERE r.incident_id = i.id ORDER BY started_at DESC LIMIT 1) lr ON true
               WHERE i.status = 'WAITING_FOR_PR_REVIEW'""").fetchall()
    changed = []
    for incident_id, url in waiting:
        new = STATUS_FOR_PR_STATE.get(get_state(url) or "") if url else None
        if new:
            set_status(incident_id, new)
            changed.append((incident_id, new))
    return changed


# ---- run log ---------------------------------------------------------------

MAX_RESULT_CHARS = 20000


def _dumps(value) -> str:
    return json.dumps(value, default=str)


def clip(value, limit: int = MAX_RESULT_CHARS):
    """Keep small values as they are; store a big one as its first `limit` characters."""
    text = _dumps(value)
    if len(text) <= limit:
        return value
    return {"truncated": True, "length": len(text), "head": text[:limit]}


class NullRunLog:
    """Does nothing. Used when no log is wanted (unit tests, quick experiments)."""
    node = "-"
    logged_messages = 0

    def event(self, kind, data=None, node=None, duration_ms=None): ...
    def tool_call(self, name, args, result, error, duration_ms): ...
    def finish(self, status, outcome=None, error=None): ...


class RunLog(NullRunLog):
    def __init__(self, incident_id: int, model: str | None = None, dsn: str | None = None):
        self.dsn = dsn or DB_DSN
        self.seq = 0
        self.node = "-"
        self.logged_messages = 0          # how many conversation messages earlier events already cover
        self._warned = False
        self.run_id = self._write("INSERT INTO agent.runs (incident_id, model) VALUES (%s, %s) RETURNING id", (incident_id, model), fetch=True)

    @classmethod
    def resume(cls, run_id: int, dsn: str | None = None) -> "RunLog":
        """Add more events to an existing run (the deploy after the merge goes into the same record)."""
        log = cls.__new__(cls)
        log.dsn, log.node, log.logged_messages, log._warned, log.run_id = dsn or DB_DSN, "-", 0, False, run_id
        log.seq = log._write("SELECT COALESCE(MAX(seq), 0) FROM agent.run_events WHERE run_id = %s", (run_id,), fetch=True) or 0
        return log

    def _write(self, sql: str, params: tuple, fetch: bool = False):
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                cursor = conn.execute(sql, params)
                return cursor.fetchone()[0] if fetch else None
        except Exception as e:                       # never let logging break the agent
            if not self._warned:
                print(f"runlog: could not write to Postgres ({e}); continuing without it", file=sys.stderr)
                self._warned = True
            return None

    def event(self, kind, data=None, node=None, duration_ms=None):
        if self.run_id is None:
            return
        self.seq += 1
        self._write(
            "INSERT INTO agent.run_events (run_id, seq, node, kind, duration_ms, data) VALUES (%s, %s, %s, %s, %s, %s)",
            (self.run_id, self.seq, node or self.node, kind, duration_ms, Jsonb(data or {}, dumps=_dumps)),
        )

    def tool_call(self, name, args, result, error, duration_ms):
        self.event("tool_call", {"tool": name, "args": args, "result": clip(result), "error": error}, duration_ms=duration_ms)

    def finish(self, status, outcome=None, error=None):
        if self.run_id is None:
            return
        self._write("UPDATE agent.runs SET status = %s, outcome = %s, error = %s, finished_at = now() WHERE id = %s",
                    (status, Jsonb(outcome, dumps=_dumps) if outcome is not None else None, error, self.run_id))


def message_record(message) -> dict:
    """A LangChain message as plain data: who said it and what."""
    record = {"role": message.type, "content": message.content}
    if getattr(message, "tool_calls", None):
        record["tool_calls"] = [{"name": c["name"], "args": c["args"], "id": c["id"]} for c in message.tool_calls]
    if getattr(message, "tool_call_id", None):
        record["tool_call_id"] = message.tool_call_id
    return record



# ---- Phoenix tracing (off unless PHOENIX_COLLECTOR_ENDPOINT is set) --------

def setup_tracing() -> bool:
    """Start sending traces to Phoenix. Call once per process, before the agent runs."""
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")
    if not endpoint:
        return False
    try:
        from phoenix.otel import register
    except ImportError:
        print("tracing: Phoenix is not installed (uv sync --extra tracing); running without it", file=sys.stderr)
        return False
    # batch=False exports each span as soon as it ends, so the view is close to live.
    register(project_name=os.environ.get("PHOENIX_PROJECT", "iam-agent"), endpoint=endpoint.rstrip("/") + "/v1/traces",
             auto_instrument=True, verbose=False)
    return True


def _tracer():
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer("iam-agent")


@contextlib.contextmanager
def run_span(incident_id: int, label: str = "agent run"):
    """One parent span around a whole agent run, so Phoenix shows the run as a
    single trace (graph nodes, LLM calls and tool calls nested inside) instead
    of dozens of unrelated ones. A no-op when tracing is off."""
    tracer = _tracer()
    if tracer is None:
        yield
        return
    with tracer.start_as_current_span(f"{label}: incident #{incident_id}") as span:
        span.set_attribute("openinference.span.kind", "AGENT")
        span.set_attribute("incident.id", incident_id)
        span.set_attribute("session.id", f"incident-{incident_id}")
        yield


@contextlib.contextmanager
def tool_span(name: str, args: dict):
    """A Phoenix span around one MCP tool call. Yields a recorder, or a no-op if tracing is off."""
    tracer = _tracer()
    if tracer is None:
        yield lambda result, error: None
        return
    with tracer.start_as_current_span(f"tool: {name}") as span:
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("tool.name", name)
        span.set_attribute("input.value", json.dumps(args, default=str)[:4000])

        def record(result, error):
            span.set_attribute("output.value", (f"ERROR: {error}" if error else json.dumps(result, default=str))[:4000])

        yield record
