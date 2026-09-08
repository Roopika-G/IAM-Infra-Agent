"""Polls public.pf_logs_raw for new ERROR/WARN rows, computes a stable
fingerprint for each, and dedups them into agent.incidents.

Never rescans from the beginning: agent.ingest_cursor holds a single-row
time bookmark, advanced in the same transaction as the incidents it
produces, so a crash mid-batch can never skip rows that weren't actually
recorded.

No pre-built "evidence bundle" here on purpose — this only persists the
fingerprint plus one representative sample message. Anything more (pod
status, events, more log context) is the live agent's own job later,
pulled on demand via MCP tools, not pre-fetched speculatively here.
"""

import os
import time

import psycopg

from signature import compute_signature

DB_DSN = os.environ.get(
    "AGENT_DB_DSN", "postgresql://postgres@localhost:5432/postgres"
)
BATCH_SIZE = 500
POLL_INTERVAL_SECONDS = 5


def parse_tag(tag: str) -> tuple[str, str]:
    """'pf.server.admin' -> ('server', 'admin'). Falls back to 'unknown'
    for anything that doesn't match the tagging scheme fluent-bit.conf
    actually uses (see helm/ping-devops/values.yaml)."""
    parts = (tag or "").split(".")
    log_type = parts[1] if len(parts) > 1 else "unknown"
    pf_role = parts[2] if len(parts) > 2 else "unknown"
    return log_type, pf_role


def poll_once(conn: psycopg.Connection) -> int:
    with conn.transaction():
        (last_seen,) = conn.execute(
            "SELECT last_seen_time FROM agent.ingest_cursor"
        ).fetchone()

        rows = conn.execute(
            """
            SELECT tag, time, data
            FROM public.pf_logs_raw
            WHERE time > %s AND data->>'level' IN ('ERROR', 'WARN')
            ORDER BY time
            LIMIT %s
            """,
            (last_seen, BATCH_SIZE),
        ).fetchall()

        if not rows:
            return 0

        max_time = last_seen
        for tag, ts, data in rows:
            max_time = max(max_time, ts)
            log_type, pf_role = parse_tag(tag)

            logger = data.get("logger")
            exception_type = data.get("exceptionClassName")
            message = data.get("message", "")

            fingerprint = compute_signature(logger, exception_type, message)

            conn.execute(
                """
                INSERT INTO agent.incidents
                    (fingerprint, log_type, pf_role, logger, exception_type,
                     sample_message, first_seen, last_seen, occurrence_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
                ON CONFLICT (fingerprint) WHERE status NOT IN ('RESOLVED', 'FAILED')
                DO UPDATE SET
                    last_seen = GREATEST(agent.incidents.last_seen, EXCLUDED.last_seen),
                    occurrence_count = agent.incidents.occurrence_count + 1
                """,
                (fingerprint, log_type, pf_role, logger, exception_type,
                 message, ts, ts),
            )

        conn.execute(
            "UPDATE agent.ingest_cursor SET last_seen_time = %s", (max_time,)
        )

    return len(rows)


def main() -> None:
    with psycopg.connect(DB_DSN, autocommit=False) as conn:
        print(f"detector.py: polling every {POLL_INTERVAL_SECONDS}s (Ctrl+C to stop)")
        while True:
            n = poll_once(conn)
            if n:
                print(f"processed {n} error/warn row(s)")
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
