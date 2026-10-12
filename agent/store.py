"""The agent's reads and writes to agent.incidents."""

import os
from pathlib import Path

import psycopg

from agent.schemas import Incident

from dotenv import load_dotenv

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
