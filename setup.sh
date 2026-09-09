#!/usr/bin/env bash
# One-time setup for a fresh clone. Brings up the whole cluster, then
# seeds the two Postgres-side pieces the agent needs.
#
# Requires infrastructure/.env already filled in (see README) -- that
# step needs real secrets (license path, pf.jwk path, admin password)
# only a human has, so it can't be scripted here.
#
# Safe to rerun: deploy-all.sh's three stages are all idempotent, and
# seed_baseline.py/ingest.py only ever add what's missing (seed_baseline
# never overwrites an existing frozen key; ingest is content-hash-gated).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "===== Cluster + Postgres + PingFederate ====="
"$ROOT_DIR/deploy-all.sh"

echo "===== Python environment ====="
cd "$ROOT_DIR"
uv sync

echo "===== Seeding agent.config_baseline + agent.agent_knowledge ====="
NAMESPACE="$(terraform -chdir="$ROOT_DIR/infrastructure" output -raw namespace)"
export AGENT_DB_DSN="postgresql://postgres:$(kubectl -n "$NAMESPACE" get secret postgres-credentials -o jsonpath='{.data.POSTGRES_JDBC_PASSWORD}' | base64 -d)@localhost:5432/postgres"

uv run python search/seed_baseline.py
uv run python search/ingest.py

echo ""
echo "Setup complete."
echo "  agent.incidents, agent.config_baseline, agent.agent_knowledge are live in Postgres."
echo "  Next: uv run python agent/detector.py to start the detector (long-running -- run it in its own terminal)."
