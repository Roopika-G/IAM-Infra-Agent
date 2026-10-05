"""Retrieval eval: labelled queries against search/query.py, measuring
whether the right knowledge chunk surfaces (recall@1 / recall@3) and, for
config faults, whether the agent would end up at the right key and file --
the whole point of retrieval here is "which config do I look up and patch".

Each case: (query, expected chunk title substring, expected related_key or None).
Queries are phrased the way real inputs arrive: raw log lines, paraphrased
symptoms, and a few direct questions. Out-of-corpus faults are NOT scored --
retrieval always returns top_k, so "no good answer" can't be measured this way.

Usage: uv run python search/eval_retrieval.py
Needs AGENT_DB_DSN (see README) and the knowledge docs ingested.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import baseline  # noqa: E402
import query  # noqa: E402

ADMIN_JDBC = "pingfederate-admin.envs.POSTGRES_JDBC_URL"
ENGINE_JDBC = "pingfederate-engine.envs.POSTGRES_JDBC_URL"

CASES = [
    # --- Sim A: the JDBC datastore fault, phrased several ways ---
    ("Unable to load custom data source instance: JDBC-FD67494D8AAFD9D8A5D00C310DE08DD078626978", "JDBC datastore failure", ADMIN_JDBC),
    ("Unable to load custom data source instance", "JDBC datastore failure", ADMIN_JDBC),
    ("DataSourceManagerImpl ERROR cannot load datasource", "JDBC datastore failure", ADMIN_JDBC),
    ("PingFederate pods are Running and Ready but database connection is failing", "JDBC datastore failure", ADMIN_JDBC),
    ("JDBC connection url points at wrong host", "JDBC datastore failure", ENGINE_JDBC),
    ("PostgresPfApp datastore cannot connect", "JDBC datastore failure", ADMIN_JDBC),
    # --- other documented behaviour ---
    ("pod is Ready but running stale config after profile path change", "SERVER_PROFILE_PATH", "pingfederate-admin.envs.SERVER_PROFILE_PATH"),
    ("server profile silently ignored, instance subdirectory not found", "SERVER_PROFILE_PATH", "pingfederate-admin.envs.SERVER_PROFILE_PATH"),
    ("unresolved ${...} placeholder ended up in live config", ".subst means substitution template", None),
    ("I pushed a config change to git but the running pod did not pick it up", "doesn't reach an already-running pod", None),
    ("why is the engine deployment using Recreate", "Recreate instead of RollingUpdate", None),
    ("why is the server profile delivered from git and not a ConfigMap", "delivered via git instead of a ConfigMap", None),
    ("can the agent automatically change the license or jwk or password", "Never auto-patch", None),
    ("what is the difference between data.json and data.json.subst", "data.json vs data.json.subst", None),
]


def main() -> int:
    r1 = r3 = key_hits = key_total = file_hits = 0
    print(f"{'#':>2}  {'rank':>4}  {'key':>3}  query")
    for i, (q, title_part, expected_key) in enumerate(CASES, 1):
        results = query.search(q, top_k=3)
        rank = next((n for n, r in enumerate(results, 1) if title_part.lower() in r["title"].lower()), None)
        r1 += rank == 1
        r3 += rank is not None

        key_ok = ""
        if expected_key:
            key_total += 1
            keys = {k for r in results for k in r["related_keys"]}
            ok = expected_key in keys
            key_hits += ok
            entry = baseline.get_baseline_entry(expected_key) if ok else None
            file_hits += bool(entry and entry["source_file"] == "helm/ping-devops/values.yaml")
            key_ok = "yes" if ok else "NO"
        print(f"{i:>2}  {rank or '-':>4}  {key_ok:>3}  {q[:78]}")

    n = len(CASES)
    print()
    print(f"chunk recall@1: {r1}/{n} = {r1 / n:.0%}")
    print(f"chunk recall@3: {r3}/{n} = {r3 / n:.0%}")
    print(f"key found in top-3 related_keys: {key_hits}/{key_total}")
    print(f"...and resolves to the right source file via config_baseline: {file_hits}/{key_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
