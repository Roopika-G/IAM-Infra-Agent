# Self-Healing IAM Agent — local dev environment

Local kind cluster running PingFederate (admin + engine) with a Postgres
(pgvector) backend. See `/Users/roopika/.claude/plans/okay-now-let-s-implement-sparkling-lamport.md`
for the full project plan.

## Prerequisites

- Docker Desktop running, with enough memory allocated (Settings → Resources)
- `terraform`, `kubectl`, `helm` on PATH
- `infrastructure/.env` set up (copy `infrastructure/.env.example`, fill in
  the license path, matching `pf.jwk` path, and administrator password)

## Starting healthy PingFederate pods

The bulk export and master key are a matched pair. `data.json.subst` contains
encrypted PingFederate values that can only be decrypted by the `pf.jwk` from
the same running instance. Do not generate a new key or mix exports from
different instances.

| Step | What to do | Important detail |
|---:|---|---|
| 1 | Put `pingfederate.lic` and `pf.jwk` in `infrastructure/`. | Both files are gitignored. The `pf.jwk` must come from the same instance as the bulk export. |
| 2 | Copy `.env.example` to `.env` and fill in all values. | The admin password must match the password in the exported configuration, or replication fails with HTTP 401. |
| 3 | Put the export at `helm/server-profile/instance/bulk-config/data.json.subst`. | Use PostgreSQL placeholders; never commit the database password. |
| 4 | Commit and push server-profile changes. | Pods clone the profile from Git, so they cannot see unpushed changes. |
| 5 | Run `./deploy-all.sh` from the repository root. | It runs Terraform, PostgreSQL, and PingFederate in the correct order. |
| 6 | Check pods and admin logs. | Both PF pods must be `1/1 Running`; bulk import and replication must return HTTP 200. |

Copy and configure the environment file:

```sh
cp infrastructure/.env.example infrastructure/.env
```

The PostgreSQL fields in `data.json.subst` must use:

```text
${POSTGRES_JDBC_URL}
${POSTGRES_JDBC_USERNAME}
${POSTGRES_JDBC_PASSWORD}
```

Deploy everything:

```sh
./deploy-all.sh
```

Verify the result:

```sh
kubectl --context kind-self-healing-iam get pods -n pingfederate
kubectl --context kind-self-healing-iam logs -n pingfederate \
  deployment/pingfederate-pingfederate-admin | \
  grep -E 'bulk/import|cluster/replicate|CONTAINER FAILURE'
```

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

### Where the admin-console password is stored

| Location | How it is stored | Committed to Git? |
|---|---|---:|
| `infrastructure/.env` | Plain text as `TF_VAR_pingfederate_admin_password` | No — gitignored |
| Terraform state | Sensitive value, but present in the local state file | No — gitignored |
| Kubernetes Secret `pingfederate-license` | Base64-encoded as `PING_IDENTITY_PASSWORD` | No |
| PingFederate pods | Injected from the Secret using `secretKeyRef` | No |
| Helm ConfigMap | Not stored here | No |

Base64 is encoding, not encryption. Protect `.env`, Terraform state,
`pingfederate.lic`, and `pf.jwk` as secrets even though they are gitignored.

Retrieve the current admin password:

```sh
kubectl --context kind-self-healing-iam -n pingfederate \
  get secret pingfederate-license \
  -o jsonpath='{.data.PING_IDENTITY_PASSWORD}' | base64 -d
echo
```

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
  server-profile/     PF server profile cloned from Git at pod startup
db/init.sql          Postgres schema (pf_app, agent) + pgvector extension
deploy-all.sh        Full deploy: terraform + platform + PF
deploy-platform.sh   Postgres only
deploy-helm.sh       PingFederate only
```
