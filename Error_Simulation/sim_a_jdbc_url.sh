#!/usr/bin/env bash
# Sim A: corrupts POSTGRES_JDBC_URL to a nonexistent host, deploys, and
# lets PF fail to connect to its custom JDBC datastore -- produces a real,
# repeated ERROR line ("Unable to load custom data source instance: ...")
# that agent/detector.py picks up and correctly dedups. Verified live
# against the cluster before this script existed -- see plan.md Phase 5's
# "Live test findings" for the original manual run (occurrence_count
# reached 4, correctly collapsed with an earlier historical occurrence of
# the identical fault).
#
# Usage:
#   ./Error_Simulation/sim_a_jdbc_url.sh inject    # break it
#   ./Error_Simulation/sim_a_jdbc_url.sh restore   # fix it
#
# Both modes edit helm/ping-devops/values.yaml then run deploy-helm.sh,
# which restarts both PF pods (Recreate strategy).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES_FILE="$ROOT_DIR/helm/ping-devops/values.yaml"

# GOLDEN_URL must match agent.config_baseline's frozen golden_value for
# pingfederate-{admin,engine}.envs.POSTGRES_JDBC_URL -- see
# search/seed_baseline.py / db/init.sql. Not read from the database here
# to keep this script dependency-free (bash + sed only, same as every
# other deploy-*.sh in this repo) -- if the golden value ever changes,
# update it in both places.
GOLDEN_URL="jdbc:postgresql://postgres.pingfederate.svc.cluster.local:5432/postgres"
BROKEN_URL="jdbc:postgresql://postgres-wrong-host.pingfederate.svc.cluster.local:5432/postgres"

MODE="${1:-}"
if [[ "$MODE" != "inject" && "$MODE" != "restore" ]]; then
  echo "usage: $0 inject|restore" >&2
  exit 1
fi

if [[ "$MODE" == "inject" ]]; then
  FROM="$GOLDEN_URL"
  TO="$BROKEN_URL"
  echo "Injecting Sim A: corrupting POSTGRES_JDBC_URL to a nonexistent host..."
else
  FROM="$BROKEN_URL"
  TO="$GOLDEN_URL"
  echo "Restoring Sim A: fixing POSTGRES_JDBC_URL back to the golden value..."
fi

sed -i.bak "s|POSTGRES_JDBC_URL: \"$FROM\"|POSTGRES_JDBC_URL: \"$TO\"|" "$VALUES_FILE"
rm -f "$VALUES_FILE.bak"

if ! grep -q "POSTGRES_JDBC_URL: \"$TO\"" "$VALUES_FILE"; then
  echo "Nothing changed -- values.yaml may already be in the target state, or the expected source value wasn't found." >&2
  exit 1
fi

echo "values.yaml updated. Deploying..."
"$ROOT_DIR/deploy-helm.sh"

echo ""
echo "Done. Both pods restart (Recreate strategy) -- watch with:"
echo "  kubectl -n pingfederate get pods -w"
if [[ "$MODE" == "inject" ]]; then
  echo ""
  echo "Once the detector has run (uv run python agent/detector.py, or a"
  echo "single poll_once() call), check for the incident:"
  echo "  SELECT fingerprint, occurrence_count, sample_message FROM agent.incidents"
  echo "  WHERE sample_message ILIKE '%data source instance%';"
fi
