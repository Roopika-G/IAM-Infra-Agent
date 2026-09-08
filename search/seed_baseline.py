"""One-time seed of agent.config_baseline from helm/ping-devops/values.yaml.

Deliberately NOT a sync script — run once, from a known-good commit, then
never re-run automatically. If it resynced on every push it could never
disagree with a bad commit, and drift detection becomes impossible by
construction (see db/init.sql's comment on config_baseline, and
knowledge/golden-architecture.md). Existing rows are never overwritten by
this script; only run it again deliberately, for a genuine re-baseline.

Extraction: walks values.yaml for any `envs:` block under a top-level
product key (pingfederate-admin.envs, pingfederate-engine.envs today) and
records each entry as "{product}.envs.{key}" -- matches the exact key
format already used in knowledge/golden-architecture.md's related_keys.
"""

import os
import subprocess
from pathlib import Path

import psycopg
import yaml

DB_DSN = os.environ.get("AGENT_DB_DSN", "postgresql://postgres@localhost:5432/postgres")
REPO_ROOT = Path(__file__).resolve().parent.parent
VALUES_PATH = REPO_ROOT / "helm" / "ping-devops" / "values.yaml"


def get_git_sha() -> str:
    return (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT)
        .decode()
        .strip()
    )


def extract_envs(doc: dict) -> dict[str, str]:
    result = {}
    for product, value in doc.items():
        if isinstance(value, dict) and isinstance(value.get("envs"), dict):
            for key, val in value["envs"].items():
                result[f"{product}.envs.{key}"] = str(val)
    return result


def main() -> None:
    doc = yaml.safe_load(VALUES_PATH.read_text())
    baseline = extract_envs(doc)
    sha = get_git_sha()

    print(f"Extracted {len(baseline)} candidate keys from {VALUES_PATH.relative_to(REPO_ROOT)} @ {sha[:8]}")

    with psycopg.connect(DB_DSN, autocommit=False) as conn:
        seeded, skipped = 0, 0
        for key, value in sorted(baseline.items()):
            row = conn.execute(
                "SELECT golden_value FROM agent.config_baseline WHERE key = %s", (key,)
            ).fetchone()
            if row is not None:
                print(f"  already frozen, not touching: {key} = {row[0]!r}")
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO agent.config_baseline (key, golden_value, source_file, captured_sha)
                VALUES (%s, %s, %s, %s)
                """,
                (key, value, "helm/ping-devops/values.yaml", sha),
            )
            print(f"  seeded: {key} = {value!r}")
            seeded += 1
        conn.commit()

    print(f"Done: {seeded} seeded, {skipped} already present (untouched).")


if __name__ == "__main__":
    main()
