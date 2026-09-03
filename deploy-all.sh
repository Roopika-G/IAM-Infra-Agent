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

echo "===== Stage 2: Helm chart ====="
"$ROOT_DIR/deploy-helm.sh" "$@"

echo "Deployment complete."
