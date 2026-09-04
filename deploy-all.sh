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

if ! docker info >/dev/null 2>&1; then
  echo "Docker doesn't appear to be running — start Docker Desktop and rerun." >&2
  exit 1
fi

echo "===== Stage 1: Infrastructure (terraform) ====="
terraform -chdir="$INFRA_DIR" init -input=false

# Docker Desktop restarts/resets can silently wipe the kind cluster's
# container while Terraform's state still thinks it exists — any
# plan/apply/refresh then fails trying to inspect a cluster that's gone.
# Detect that specific case and self-heal: drop every cluster-scoped
# resource from state so this run just recreates them cleanly, instead of
# needing a manual `terraform state rm` every time it happens. Safe: these
# reads (state list, output) only touch the local state file, never the
# (possibly-gone) cluster itself.
if terraform -chdir="$INFRA_DIR" state list 2>/dev/null | grep -q '^kind_cluster\.'; then
  CLUSTER_NAME="$(terraform -chdir="$INFRA_DIR" output -raw cluster_name 2>/dev/null || true)"
  if [ -n "$CLUSTER_NAME" ] && ! docker ps -a --format '{{.Names}}' | grep -qx "${CLUSTER_NAME}-control-plane"; then
    echo "Stale state: kind cluster '$CLUSTER_NAME' is tracked but its container is gone — cleaning up state."
    terraform -chdir="$INFRA_DIR" state list 2>/dev/null \
      | grep -E '^(kind_cluster\.|kubernetes_)' \
      | while IFS= read -r resource; do
          terraform -chdir="$INFRA_DIR" state rm "$resource"
        done
  fi
fi

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
