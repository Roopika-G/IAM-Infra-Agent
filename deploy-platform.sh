#!/usr/bin/env bash
# One-time platform bootstrap: Postgres (with pgvector).
#
# Never redeployed by the agent at runtime — it's static infrastructure a
# human stands up once. Applied as a raw manifest (helm/postgres.yaml, via
# kubectl) rather than a Helm chart: it uses the official pgvector/pgvector
# image, and no maintained Helm chart pairs cleanly with that image for a
# simple single-instance deployment.
#
# Postgres does triple duty here that would otherwise be OpenSearch's job:
# pgvector for the agent-knowledge/incident-history embeddings (Phases
# 7/13), a plain table for pf-logs (Phase 4, via Fluent Bit's pgsql output
# plugin), and tsvector/GIN full-text search for the keyword half of hybrid
# retrieval.
#
# Requires `terraform apply` to have been run first in infrastructure/
# (cluster, namespace, and the postgres-credentials secret must already
# exist). See push-to-github.sh for the separate (unrelated to the cluster)
# step of pushing this repo to GitHub as the iam-platform repo.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$ROOT_DIR/infrastructure"

NAMESPACE="${NAMESPACE:-$(terraform -chdir="$INFRA_DIR" output -raw namespace)}"
KUBECTL_CONTEXT="${KUBECTL_CONTEXT:-$(terraform -chdir="$INFRA_DIR" output -raw kubectl_context)}"

kubectl config use-context "$KUBECTL_CONTEXT"

echo "===== Postgres (pgvector) ====="
kubectl apply -n "$NAMESPACE" -f "$ROOT_DIR/helm/postgres.yaml"

kubectl -n "$NAMESPACE" wait --for=condition=Ready pod \
  -l app=postgres --timeout=300s

echo "Applying db/init.sql (idempotent)..."
PG_ADMIN_PASSWORD="$(kubectl -n "$NAMESPACE" get secret postgres-credentials -o jsonpath='{.data.POSTGRES_JDBC_PASSWORD}' | base64 -d)"
PG_POD="$(kubectl -n "$NAMESPACE" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "$NAMESPACE" exec -i "$PG_POD" -- env PGPASSWORD="$PG_ADMIN_PASSWORD" \
  psql -U postgres -v ON_ERROR_STOP=1 < "$ROOT_DIR/db/init.sql"

echo "Platform bootstrap complete."
echo "  Postgres: schemas pf_app, agent + pgvector extension (namespace $NAMESPACE)"
