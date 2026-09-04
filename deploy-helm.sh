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
CHART_DIR="$ROOT_DIR/helm/ping-devops"

kubectl config use-context "$KUBECTL_CONTEXT"

helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  "$@"

# PingFederate only git-clones its server profile (SERVER_PROFILE_URL, see
# helm/ping-devops/values.yaml) at its own pod startup — a profile change
# pushed to that repo doesn't reach a pod that's already running, and if
# nothing in values.yaml changed either, `helm upgrade` above is a no-op
# that wouldn't restart anything on its own. Force one explicitly so a
# profile edit always actually takes effect on the next deploy.
kubectl rollout restart deployment \
  --namespace "$NAMESPACE" \
  -l "app.kubernetes.io/instance=$RELEASE_NAME"

echo "Helm release '$RELEASE_NAME' deployed to namespace '$NAMESPACE' (context: $KUBECTL_CONTEXT)."
