# Self-Healing IAM Agent — local dev environment

Local kind cluster running PingFederate (admin + engine) with a Postgres
(pgvector) backend. See `/Users/roopika/.claude/plans/okay-now-let-s-implement-sparkling-lamport.md`
for the full project plan.

## Prerequisites

- Docker Desktop running, with enough memory allocated (Settings → Resources)
- `terraform`, `kubectl`, `helm` on PATH
- `infrastructure/.env` set up (copy `infrastructure/.env.example`, fill in
  your PingFederate license path)

## Deploy everything

```sh
./deploy-all.sh
```

Runs, in order: Terraform (cluster, namespace, secrets) → Postgres
(`deploy-platform.sh`) → PingFederate (`deploy-helm.sh`). Safe to rerun —
every stage is idempotent.

To redeploy just one piece after making changes:

```sh
./deploy-platform.sh   # Postgres only
./deploy-helm.sh       # PingFederate only (also picks up server-profile edits)
```

## Connecting to the cluster

The kind cluster isn't your default kubectl context — point `kubectl` at it
explicitly, or switch your current context:

```sh
kubectl --context kind-self-healing-iam get pods -n pingfederate

# or, to make it the default so you can drop --context:
kubectl config use-context kind-self-healing-iam
kubectl get pods -n pingfederate
```

(`deploy-all.sh`/`deploy-helm.sh`/`deploy-platform.sh` already do this
switch themselves before running — this is only for when you want to run
`kubectl`/`helm` by hand afterward.)

## Seeing what's running

```sh
kubectl -n pingfederate get pods                 # all pods
kubectl -n pingfederate get pods -w               # watch, live-updating
kubectl -n pingfederate get deploy,statefulset    # workload objects
kubectl -n pingfederate logs -f <pod-name>        # follow logs
kubectl -n pingfederate describe pod <pod-name>   # events, why a pod isn't Ready
kubectl -n pingfederate exec -it <pod-name> -- sh # shell into a pod
```

Current pods you should see: `pingfederate-pingfederate-admin-*`,
`pingfederate-pingfederate-engine-*`, `postgres-0`.

## Accessing services from your host

| Service | URL / connection | Notes |
|---|---|---|
| PingFederate admin console | https://localhost:9999/pingfederate/app | self-signed cert, browser will warn |
| Postgres | `psql -h localhost -p 5432 -U postgres` | password: see below |

Get the Postgres password:

```sh
kubectl -n pingfederate get secret postgres-credentials \
  -o jsonpath='{.data.POSTGRES_JDBC_PASSWORD}' | base64 -d
```

## Terraform (infrastructure only — cluster, namespace, secrets)

```sh
cd infrastructure
source .env
terraform plan     # preview
terraform apply    # apply (deploy-all.sh does this for you)
```

If a `kind_config` change forces the cluster to be replaced, apply the
cluster on its own first, then everything else — `deploy-all.sh` already
does this automatically:

```sh
terraform apply -target=kind_cluster.this
terraform apply
```

## Tearing down

```sh
cd infrastructure
source .env
terraform destroy
```

This deletes the kind cluster (and everything in it — Postgres data
included, nothing persists outside the cluster).

## Repo layout

```
infrastructure/     Terraform — cluster, namespace, secrets
helm/
  ping-devops/       Vendored PingFederate Helm chart
  postgres.yaml       Raw manifest (pgvector/pgvector image)
  server-profile/     PF server profile, mounted via ConfigMap
db/init.sql          Postgres schema (pf_app, agent) + pgvector extension
deploy-all.sh        Full deploy: terraform + platform + PF
deploy-platform.sh   Postgres only
deploy-helm.sh       PingFederate only
```
