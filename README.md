# Self-Healing IAM Agent — local dev environment

Local kind cluster running PingFederate (admin + engine) with a Postgres
(pgvector) backend, plus the beginnings of the self-healing agent itself
(log ingestion, error-signature dedup, a RAG knowledge base). See `plan.md`
for the full project plan and current status.

## Prerequisites

- Docker Desktop running, with enough memory allocated (Settings → Resources)
- `terraform`, `kubectl`, `helm`, `uv` on PATH
- `gh` authenticated (`gh auth status`) — `helm/server-profile/**` is
  git-sourced, PF pods pull it from GitHub at startup, so pushing there has
  to actually work
- `infrastructure/.env` set up (copy `infrastructure/.env.example`, fill in
  the license path, matching `pf.jwk` path, and administrator password)

## First-time setup (cloning this repo fresh)

1. Prerequisites above, then `cp infrastructure/.env.example infrastructure/.env` and fill it in (see "Starting healthy PingFederate pods" below) — the one step that can't be scripted, since it needs your own license/jwk paths and admin password.
2. `./setup.sh` — everything else, one command:
   - `deploy-all.sh` (kind cluster → Postgres + schema → PingFederate)
   - `uv sync` (Python env — first run of `search/ingest.py` also downloads a small (~130MB) local embedding model, one-time)
   - `search/seed_baseline.py` (freezes the known-good config baseline)
   - `search/ingest.py` (embeds `knowledge/golden-architecture.md`)

Safe to rerun any time — every step it calls is idempotent (`seed_baseline.py` only adds keys not already frozen; `ingest.py` only re-embeds chunks that actually changed). One side effect worth knowing: it always restarts both PF pods, since `deploy-helm.sh` unconditionally forces a rollout restart regardless of whether anything actually changed.

After this, `agent.incidents`, `agent.config_baseline`, and `agent.agent_knowledge` are all live in Postgres — see "What to rerun when you change things" below for ongoing work.

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

## What to rerun when you change things

| You changed... | Run this | Why |
|---|---|---|
| `infrastructure/*.tf` (cluster/namespace/secrets) | `./deploy-all.sh` (or `cd infrastructure && terraform apply`) | Idempotent — safe any time |
| `helm/ping-devops/values.yaml` (sidecars, envs, resources, etc.) | `./deploy-helm.sh` | Applies the new Helm values and restarts the pods |
| `helm/server-profile/**` (PF config, `log4j2.xml`, etc.) | `git push` **then** `./deploy-helm.sh` | Git-sourced — PF pulls this from GitHub at pod startup, so a local edit alone does nothing until it's pushed *and* the pods restart |
| `db/init.sql` (new tables/columns) | `./deploy-platform.sh` | Idempotent (`CREATE TABLE IF NOT EXISTS`, etc.) — safe to rerun any time, only applies what's actually new |
| `knowledge/*.md` | `uv run python search/ingest.py` | Content-hash-gated — only re-embeds chunks that actually changed, safe to rerun any time |
| A **new** key added to `values.yaml`'s `envs:` | `uv run python search/seed_baseline.py` | Only seeds keys not already frozen — never touches or updates an existing baseline row, deliberately (see below) |
| Nothing — just want the detector running | `uv run python agent/detector.py` | Long-running poll loop, not a setup step — leave it running in its own terminal |

**Never run `search/seed_baseline.py` just because an existing value changed in `values.yaml`.** `agent.config_baseline` is a frozen reference used to catch drift — if it resynced on every change it could never disagree with a bad commit, and drift detection becomes impossible by construction. It's the one script here that's deliberately *not* idempotent-toward-updates. See the `config_baseline` comment in `db/init.sql` for the full reasoning.

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

## Running a fault simulation

`Error_Simulation/` holds scripts that deliberately break PF, to test
whether `agent/detector.py` actually catches it. Each one has two modes —
`inject` (break it) and `restore` (fix it) — run as two separate,
deliberate steps so you have time to observe the broken state in between,
not a script that blinks the fault on and off automatically.

### Sim A — `POSTGRES_JDBC_URL` corruption

```sh
./Error_Simulation/sim_a_jdbc_url.sh inject
```

Corrupts PF's JDBC datastore URL to a nonexistent host and redeploys.
Both pods stay `2/2 Running` — this fault doesn't crash PF, it just makes
one specific datastore connection fail, logging a real `ERROR`. Watch it
come back up:

```sh
kubectl -n pingfederate get pods -w
```

Run the detector against it (one poll is enough for a manual check):

```sh
export AGENT_DB_DSN="postgresql://postgres:$(kubectl -n pingfederate get secret postgres-credentials -o jsonpath='{.data.POSTGRES_JDBC_PASSWORD}' | base64 -d)@localhost:5432/postgres"
uv run python -c "
import sys; sys.path.insert(0, 'agent')
import detector, psycopg
with psycopg.connect(detector.DB_DSN) as conn:
    print('processed', detector.poll_once(conn), 'rows')
    conn.commit()
"
```

Then check it landed as a deduped incident (in pgAdmin, or `psql`):

```sql
SELECT fingerprint, occurrence_count, sample_message
FROM agent.incidents
WHERE sample_message ILIKE '%data source instance%';
```

When you're done, put PF back:

```sh
./Error_Simulation/sim_a_jdbc_url.sh restore
```

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
infrastructure/      Terraform — cluster, namespace, secrets
helm/
  ping-devops/        Vendored PingFederate Helm chart (also defines the
                       Fluent Bit sidecar — see values.yaml's sidecars:)
  postgres.yaml        Raw manifest (pgvector/pgvector image)
  server-profile/      PF server profile cloned from Git at pod startup
db/init.sql           Postgres schema: pf_app, agent (pf_logs_raw lives in
                       public — see its comment in init.sql for why) +
                       pgvector extension
knowledge/            RAG source docs (golden-architecture.md today)
agent/
  signature.py         normalizes + fingerprints a log message
  detector.py           polls pf_logs_raw, dedups into agent.incidents
  embeddings.py         local embeddings (BAAI/bge-small-en-v1.5)
search/
  ingest.py              embeds knowledge/*.md into agent.agent_knowledge
  seed_baseline.py        one-time: freezes agent.config_baseline
  query.py                 hybrid (keyword+vector) search over agent_knowledge
  baseline.py               exact-key lookup against config_baseline
tests/                Python tests (pytest)
pyproject.toml        Python deps, managed with uv
Error_Simulation/
  sim_a_jdbc_url.sh    Sim A: inject/restore a POSTGRES_JDBC_URL corruption
setup.sh              First-time setup: deploy-all.sh + uv sync + seed_baseline.py + ingest.py
deploy-all.sh         Full deploy: terraform + platform + PF
deploy-platform.sh    Postgres only
deploy-helm.sh        PingFederate only
```
