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

# Two-phase apply: the kubernetes provider's config (host/certs) comes from
# kind_cluster.this's own attributes, which aren't known until the cluster
# actually exists. Whenever kind_config changes force a cluster replace,
# Terraform can't even build a plan for the kubernetes-provider resources
# in one pass — it tries to use the not-yet-known new values and fails.
# Targeting the cluster alone first, then applying everything else once its
# real endpoint/certs are in state, avoids this. Harmless no-op via -target
# when no cluster replacement is actually pending.
terraform -chdir="$INFRA_DIR" apply -target=kind_cluster.this -input=false
terraform -chdir="$INFRA_DIR" apply -input=false

echo "===== Stage 2: Platform (Postgres) ====="
# Must run before the helm chart — PingFederate's JDBC datastore connects
# to Postgres at its own startup, with no wait-for/retry on it, so Postgres
# needs to already be up first.
"$ROOT_DIR/deploy-platform.sh"

echo "===== Stage 3: Helm chart (PingFederate) ====="
"$ROOT_DIR/deploy-helm.sh" "$@"

echo "Deployment complete."
