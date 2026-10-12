"""The run log: a complete record of what the agent did, written to Postgres.

One row in agent.runs per agent invocation, and one row in agent.run_events per
step, in order. Events are written as they happen, so a crash still leaves a
trail. A logging failure must never break the agent: it is reported once on
stderr and the run carries on.

Event kinds: node_start, node_end, prompt, snapshot, llm_call, tool_call,
evidence, decision, policy, outcome, error.
"""

import json
import sys
import time

import psycopg
from psycopg.types.json import Jsonb

from agent.store import DB_DSN       # importing store also loads the repo .env

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


class Stopwatch:
    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.ms = int((time.monotonic() - self.start) * 1000)
