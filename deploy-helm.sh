#!/usr/bin/env bash
# Stage 2: installs/upgrades the PingFederate helm chart.
# Requires `terraform apply` to have been run first in infrastructure/
# (cluster, namespace, and devops-secret must already exist).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$ROOT_DIR/infrastructure"
PROFILE_DIR="$ROOT_DIR/helm/server-profile/instance"

NAMESPACE="${NAMESPACE:-$(terraform -chdir="$INFRA_DIR" output -raw namespace)}"
KUBECTL_CONTEXT="${KUBECTL_CONTEXT:-$(terraform -chdir="$INFRA_DIR" output -raw kubectl_context)}"
RELEASE_NAME="${RELEASE_NAME:-pingfederate}"
CHART_DIR="$ROOT_DIR/helm/ping-devops"

kubectl config use-context "$KUBECTL_CONTEXT"

# PingFederate's server profile becomes a ConfigMap via the chart's own
# generic configMaps: values key (templates/configmaps/configmaps.yaml) —
# --set-file loads each local file's content in at upgrade time, so the
# ConfigMap is a normal part of this one `helm upgrade`, not a separate
# imperative step. Mounted at /opt/in/instance/... by
# helm/ping-devops/values.yaml, same name this creates.
helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  --set-file 'configMaps.pingfederate-server-profile.data.run\.properties\.subst'="$PROFILE_DIR/bin/run.properties.subst" \
  --set-file 'configMaps.pingfederate-server-profile.data.data\.json\.subst'="$PROFILE_DIR/bulk-config/data.json.subst" \
  "$@"

# Kubernetes never restarts a pod just because a ConfigMap it mounts
# changed content — true regardless of Helm/kubectl/anything else managing
# that ConfigMap. This chart also has no built-in checksum-annotation hook
# to force a rollout on config change (checked — only exists for
# Vault-related annotations). So: force one explicitly, every deploy,
# so a profile edit always actually takes effect.
kubectl rollout restart deployment \
  --namespace "$NAMESPACE" \
  -l "app.kubernetes.io/instance=$RELEASE_NAME"

echo "Helm release '$RELEASE_NAME' deployed to namespace '$NAMESPACE' (context: $KUBECTL_CONTEXT)."
