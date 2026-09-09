"""Exact-match lookup against agent.config_baseline.

No embedding, no similarity search -- the agent calls this once it
already knows the exact key it's checking (either because it knows PF
config, or because a search/query.py hit's related_keys told it). See
db/init.sql's comment on config_baseline for why this table is frozen and
never auto-resynced.
"""

import os

import psycopg

DB_DSN = os.environ.get("AGENT_DB_DSN", "postgresql://postgres@localhost:5432/postgres")


def get_baseline_value(key: str) -> str | None:
    """Returns the frozen golden value for an exact key, or None if the
    key isn't in the baseline at all."""
    with psycopg.connect(DB_DSN) as conn:
        row = conn.execute(
            "SELECT golden_value FROM agent.config_baseline WHERE key = %s", (key,)
        ).fetchone()
    return row[0] if row else None


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: uv run python search/baseline.py <key>")
        raise SystemExit(1)
    value = get_baseline_value(sys.argv[1])
    print(value if value is not None else f"(no baseline row for key {sys.argv[1]!r})")
