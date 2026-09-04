#!/usr/bin/env bash
# Runs the full deployment in order: infrastructure (terraform) first, then
# the helm chart. Any extra args are forwarded to `helm upgrade --install`
# (e.g. -f custom-values.yaml).
#
# Requires TF_VAR_pingfederate_license_path to be exported before running
# (see infrastructure/.env.example).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$ROOT_DIR/infrastructure"

echo "===== Stage 0: Environment variables ====="
source "$INFRA_DIR/.env"

echo "===== Stage 1: Infrastructure (terraform) ====="
terraform -chdir="$INFRA_DIR" init -input=false
terraform -chdir="$INFRA_DIR" apply -input=false

echo "===== Stage 2: Platform (Postgres) ====="
# Must run before the helm chart — PingFederate's JDBC datastore connects
# to Postgres at its own startup, with no wait-for/retry on it, so Postgres
# needs to already be up first.
"$ROOT_DIR/deploy-platform.sh"

echo "===== Stage 3: Helm chart (PingFederate) ====="
"$ROOT_DIR/deploy-helm.sh" "$@"

echo "Deployment complete."
