#!/usr/bin/env bash
# Stage 2: installs/upgrades the PingFederate helm chart.
# Requires `terraform apply` to have been run first in infrastructure/
# (cluster, namespace, and devops-secret must already exist).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$ROOT_DIR/infrastructure"

NAMESPACE="${NAMESPACE:-$(terraform -chdir="$INFRA_DIR" output -raw namespace)}"
KUBECTL_CONTEXT="${KUBECTL_CONTEXT:-$(terraform -chdir="$INFRA_DIR" output -raw kubectl_context)}"
RELEASE_NAME="${RELEASE_NAME:-pingfederate}"
CHART_DIR="$ROOT_DIR/charts/ping-devops"

kubectl config use-context "$KUBECTL_CONTEXT"

helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  "$@"

echo "Helm release '$RELEASE_NAME' deployed to namespace '$NAMESPACE' (context: $KUBECTL_CONTEXT)."
