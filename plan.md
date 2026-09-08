# Self-Healing IAM Agent — Implementation Plan (v1)

## Context

The user handed over a full v1 spec for a self-healing IAM agent: two fault simulations against a PingFederate deployment, log/state-driven detection, an OpenSearch-backed knowledge base and incident history, a LangGraph agent with a human approval gate, Gitea-hosted PR/merge, and an offline eval harness. The goal of this planning pass was to reconcile that spec against what already exists in `/Users/roopika/Documents/Agent_Code` (a working kind cluster + Terraform + a vendored PingFederate Helm chart, built in an earlier session) and turn the spec's own 14-phase build order into something concretely actionable against this repo, instead of a generic greenfield plan.

Four architecture questions were resolved with the user before finalizing:

1. **Docker memory** — Desktop's VM was capped at 7.65GB; user raised it before Phase 2 work started. (Turned out to matter less than expected — see the OpenSearch revision below.)
2. **Helm pattern** — keep the existing helm-CLI-via-shell-script pattern (`deploy-helm.sh`'s shape) for every new release. No Terraform helm provider gets added; Terraform stays scoped to cluster/namespace/secrets.
3. **iam-platform repo** — ~~once Gitea is up, mirror this same repo's current content into it~~ superseded, see below: this is a real GitHub repo now, not Gitea.
4. **Build order** — follow the spec's own Section 9 phase order as-is, adjusted only where phases are already partly done.

Revised twice more since, both mid-Phase-2:

- **OpenSearch dropped entirely**, replaced by Postgres's pgvector extension (embeddings as a native column type) plus tsvector/GIN full-text search — one datastore instead of two. `pf-logs-*` becomes a plain Postgres table that Fluent Bit writes to via its official pgsql output plugin. Full reasoning in Phase 2 below.
- **Gitea dropped entirely**, replaced by real GitHub. The original spec's case for Gitea was self-containment (no external account/token needed, fully offline-reproducible demo). The user preferred using their own GitHub account instead — `gh` was already authenticated locally (Roopika-G, repo scope) at the time this was decided. This also means Phase 9/10's `repo_config`/PR-merge tooling can lean on the existing GitHub MCP server instead of hand-building Gitea API calls — less custom code, not more.

Live cluster state was also verified during planning (not just static repo inspection): `kind-self-healing-iam` is up with 1 node, and the `pingfederate` Helm release is deployed with both admin and engine pods `Running`. Neither pod currently sources its server profile from git — the chart's `SERVER_PROFILE_URL`/`SERVER_PROFILE_PATH` mechanism exists in the vendored image/chart schema but is unset.

Revised during Phase 1 execution: the spec's stack table lists "1 control-plane + 2 workers," but kind nodes are just containers sharing the same Docker Desktop VM's CPU/RAM — multiple nodes don't add capacity, only per-node kubelet/kube-proxy/containerd overhead, which cuts against the already-tight memory budget. Nothing in this project's actual detection/remediation logic depends on multi-node topology (the sims are app-level config drift, not node failures). Decision: stay single-node, documented as a comment in `infrastructure/main.tf`.

**Reconciliation pass (during Phase 2/RAG design work):** the user separately drafted a much more detailed v1 spec (now `README.md`) covering PostgreSQL table design, a golden architecture RAG document, baseline governance, SPIFFE/SPIRE identity, and a local OAuth authorization service. Comparing it against this plan surfaced several decisions, resolved as follows:

- **LLM: hosted API, not local Ollama.** The user provides API keys. `agent/llm.py` (Phase 8) stays provider-abstracted/env-driven as already planned — this just confirms it targets a hosted API, not a local model.
- **Git hosting: GitHub MCP server, reaffirmed.** No Forgejo. The already-implemented `SERVER_PROFILE_URL`/`push-to-github.sh` wiring against GitHub stands.
- **Fluent Bit pipeline: two-stage, not one-stage.** The README's own table design (`raw_json` + `processed` boolean columns) implies Fluent Bit only does raw ingest, and a Python pass computes `fingerprint`/parses structured fields afterward — Fluent Bit's pgsql output can't reliably do hashing/regex extraction itself. Applied to Phase 4/5 below.
- **Agent write-scope narrowed — adopted.** The agent's job now ends at PR creation (Phase 10, rewritten). Merge, deploy, and verify move to a CI/CD pipeline gated by a SPIFFE/SPIRE-authenticated, commit-bound scoped token from a local OAuth authorization service — both new (Phase 11, inserted), with deploy/verify itself becoming its own phase (Phase 12, inserted). This is a real security improvement (the agent never merges its own PR or holds standing deploy credentials) and was worth the added phases.
- **Incident-history learning (old Phase 13): kept as V1**, not deferred to V2 as the README's non-goals list suggested. Renumbered, content unchanged.
- **Eval harness (old Phase 14): kept as V1**, not dropped. Renumbered, content unchanged.
- **Simulation count: staying at two** (Sim A profile-path trap, Sim B `ImagePullBackOff`), not expanded to the README's seven lettered scenarios. The README's extra scenarios (JDBC URL, JDBC placeholder, OOM, profile-not-applied, protected-credential escalation) are good V2 candidates but out of scope for this plan.

## Phase 1 — kind + Terraform (namespace, secrets, storage) — done

Status: cluster is live, `terraform apply` is clean and idempotent, single control-plane node (deliberately — see note above). Storage: kind's default standard StorageClass (local-path-provisioner) is sufficient, no `kubernetes_storage_class` resource needed — documented in a comment in `main.tf`. Namespace `pingfederate` stays as-is; Phase 2's Postgres reuses this same namespace rather than a second one — also documented in `main.tf`. (Whether to split namespaces further per-component is an open question the user raised but hasn't confirmed yet — not acted on.) No further work needed for this phase.

**Exit criteria (met):** `terraform plan` (with `infrastructure/.env` sourced) reports no changes; PF pods remain `Running`.

## Phase 2 — Postgres (pgvector) + push repo to GitHub

Revised after Phase 2 was drafted: OpenSearch is gone entirely, replaced by Postgres doing triple duty via the pgvector extension. Reasoning: pgvector stores embeddings as a native Postgres column type with similarity search built in — since Postgres was already in the stack, this means "vectors in a container with a mount" with no second service. Scope of the replacement, confirmed with the user: all three of OpenSearch's jobs move to Postgres, not just the vector-search one:

- `agent-knowledge` / `incident-history` (Phases 7/13) → Postgres tables with a vector column (pgvector) + tsvector/GIN full-text index, manually blended for the keyword+vector hybrid search the spec wants (approximating OpenSearch's BM25+kNN RRF merge).
- `pf-logs-*` (Phase 4) → a plain Postgres table. Fluent Bit ships to it directly via its official pgsql output plugin (stores each record as JSONB via libpq) instead of an OpenSearch bulk output — confirmed this plugin exists and is documented before committing to this design.

Bitnami's `postgresql` chart doesn't ship pgvector (confirmed — it's not in the standard image, and third-party "Bitnami-compatible" pgvector images are unofficial forks not worth building on). Postgres is therefore no longer a Helm release at all: it's a raw Kubernetes manifest (`helm/postgres.yaml`, applied via `kubectl apply`, not `helm upgrade --install`) using the official `pgvector/pgvector` image, which is a drop-in for the standard `postgres` image (identical entrypoint/env vars) with the extension precompiled — confirmed against the pgvector project's own README before choosing this over heavier alternatives (a Postgres operator like CloudNativePG, which GKE's own docs use, was rejected as more moving parts than a single-node local demo needs).

**Entry criteria:** Phase 1 done. (The Docker-memory pressure that originally motivated bumping to 20GB+ was mostly about OpenSearch's 1GB heap + JVM overhead — with OpenSearch gone, memory headroom is much less tight, though the bump already happened and doesn't need reverting.)

Also revised: Gitea dropped for real GitHub (see Context above — the user's preference, `gh` already authenticated locally). This actually simplifies Phase 9/10 later: `repo_config`'s PR/merge tooling can use the existing GitHub MCP server instead of hand-building Gitea API calls. Pushing to GitHub is also unrelated to the kind cluster (no kubectl/terraform involved), so it's a separate script rather than folded into `deploy-platform.sh`.

Important distinction driving this phase's structure: Postgres is one-time platform bootstrap — a human deploys it once via `deploy-all.sh`/`deploy-platform.sh` and the agent never redeploys it at runtime. This is different from PingFederate's own release, which the agent does redeploy at runtime in Phase 9/10 (via the `helm_ops` MCP server, calling the same `helm upgrade --install pingfederate ...` shape `deploy-helm.sh` already runs — that MCP server is Python subprocessing `helm` directly, it doesn't shell out to `deploy-helm.sh` itself, since the agent needs structured results, not a shell exit code).

**Deliverables** (all implemented; files written, nothing deployed/pushed yet):

- `deploy-platform.sh` — applies `helm/postgres.yaml` via `kubectl apply`, waits for it Ready, applies `db/init.sql`. All Helm-related files, including the vendored `helm/ping-devops` chart, live under one `helm/` directory rather than scattered at repo root. Keep `deploy-helm.sh` (PingFederate) as its own file, unchanged — different lifecycle, and the one thing Phase 9 wraps later.
- `helm/postgres.yaml` — raw Service (NodePort) + StatefulSet manifest, `pgvector/pgvector:0.8.6-pg17` image, password via `POSTGRES_PASSWORD_FILE` reading a mounted `postgres-credentials` secret volume (not a plain env var), 4Gi PVC.
- Postgres credentials: `kubernetes_secret.postgres_credentials` in `infrastructure/main.tf` (via `random_password`; added `hashicorp/random` to `infrastructure/versions.tf`). Output `postgres_secret_name` in `infrastructure/outputs.tf`. A NodePort port-mapping (`postgres_port`/`postgres_node_port` vars, default host 5432) so psql/TablePlus/pgAdmin can reach it directly.
- `db/init.sql` — `CREATE EXTENSION IF NOT EXISTS vector;`, schema `pf_app` (PF's JDBC datastore) and schema `agent` (incident state/audit tables, extended in Phase 5; will also hold `pf_logs`, `agent_knowledge`, and `incident_history` tables in Phases 4/7/13). Applied via `kubectl exec ... psql` every run of `deploy-platform.sh` (idempotent — safe to rerun against an existing database, and is how future schema changes get applied too, since the official postgres image's `/docker-entrypoint-initdb.d` auto-run mechanism only fires on an empty PVC).
- `push-to-github.sh` (new, separate script — no cluster/terraform involved) — `gh repo create iam-platform` (private by default) if it doesn't already exist, adds a `github` remote, pushes HEAD to `main`. Idempotent (safe to rerun). Whether the repo stays private affects Phase 3b: PingFederate's server-profile-from-git pull will need a token if private, or none if made public — not yet decided, flagged there.

**Exit criteria:** Postgres pod `Running`; `psql` shows schemas `pf_app`/`agent` and `SELECT * FROM pg_extension WHERE extname = 'vector'` returns a row; GitHub has an `iam-platform` repo whose `main` matches this repo's current git log; `git remote -v` shows `github`.

**Risks:** none outstanding for Postgres. For GitHub: decide public vs. private before Phase 3b needs an answer (see above).

## Phase 3 — PF reaches Ready with profile sourced from GitHub (two sub-steps)

The ordering wrinkle is smaller now than with Gitea: GitHub is an external service that's always "up" — the only dependency is whether `push-to-github.sh` has been run yet (Phase 2), which needs nothing from the cluster and can happen any time. So:

- **3a (already done):** PF admin+engine reach Ready via `helm/ping-devops`, license mounted via the existing `kubernetes_secret.pingfederate_license`. No further work.
- **3b (blocked on `push-to-github.sh` having run):** add `envs:` blocks with `SERVER_PROFILE_URL`/`SERVER_PROFILE_PATH` under `pingfederate-admin:` and `pingfederate-engine:` in `helm/ping-devops/values.yaml` (or a separate override file — `infrastructure/main.tf` already has a comment pointing at `helm/pingfederate-values.yaml`, which doesn't currently exist; either create it now and use it for this, or treat the comment as stale and edit the vendored chart's values directly — worth a quick decision when this sub-step starts). Point the URL at `https://github.com/<owner>/iam-platform` — a plain public HTTPS URL, no in-cluster DNS concerns the way Gitea had. Depends on the still-open public/private decision from Phase 2: if `iam-platform` is private (the script's default), the PF pod needs a GitHub token to clone it (a `kubernetes_secret` mounted the same way the license is); if public, no auth needed at all. Simplest path: make the repo public once Phase 3b starts, unless there's a reason not to.
  - Populate `helm/server-profile/` with a real exported PF profile.
  - Identify and hand-verify the Sim A property now, per the spec's own instruction — pick an allowlisted profile key that fails deterministically at startup with a clean, greppable log line. If nothing in the real profile qualifies, fall back to the spec's suggested init-container validator that exits non-zero with a structured JSON log line on a bad allowlisted key.

**Exit criteria:** PF pods pull their profile from GitHub (confirm via pod logs showing a successful git-sync from the `iam-platform` URL); corrupting the identified Sim-A property and redeploying produces a reproducible NotReady/CrashLoopBackOff with a known, greppable error line — write that line down, it's the direct input to Phase 4.

**Risks:** spec itself flags this as the longest phase (PF licensing + profile plumbing).

## Phase 4 — Fluent Bit → Postgres `agent.pf_logs_raw` — done

Revised: no OpenSearch — Fluent Bit ships straight to Postgres via its official `pgsql` output plugin. Confirmed for real (not assumed): the plugin is compiled into the stock `fluent/fluent-bit` image (`-DFLB_OUT_PGSQL=On` in its official Dockerfile), and it works by auto-creating a **fixed** three-column table (`tag TEXT, time TIMESTAMP, data JSONB`) — it cannot target a custom multi-column schema, and any per-record fields added by a filter land inside `data`, not as separate SQL columns. This directly shaped the schema below.

Revised again: **sidecar, not DaemonSet.** A DaemonSet would mean reading PF's logs off the node filesystem with its own RBAC and no natural tie to PF's own output directory. Instead, Fluent Bit runs as a second container inside each `pingfederate-admin`/`pingfederate-engine` pod (the chart's existing `sidecars:`/`includeSidecars:` mechanism, previously unused), sharing PF's own `/opt/out` via a hand-declared `out-dir` `emptyDir` (the chart only auto-creates that volume when `utilitySidecar.enabled`, which we don't want). Because it's a sidecar and already knows its own pod identity via the Kubernetes downward API, tagging uses a `record_modifier` filter (`pf_role`, `namespace`, `pod_name` from env), not a `Kubernetes` filter — that's the DaemonSet-specific tool for node-wide `/var/log/containers` metadata lookup, wrong fit here.

Also revised: Fluent Bit does **not** compute `error_signature`/`fingerprint` — it has no crypto library and only clunky Lua string handling, so hashing/regex extraction is unreliable and hard to test inside it. That work is entirely Phase 5's, in Python, against the raw table.

Also required, not anticipated in the original spec: PingFederate ships with JSON log appenders **already defined but disabled**, and the JSON variant collides with the plain-text one on the exact same file path (`FILE`/`FILE-JSON` both default to `${sys:pf.log.dir}/server.log`) — confirmed by pulling the real `log4j2.xml` off the live admin pod rather than guessing at Log4j2 XML syntax against a production-critical config. Fixed by re-pointing `FILE-JSON`'s `fileName`/`filePattern` to `server-json.log` (same for `AdminApiAudit-JSON` → `admin-api-json.log`) and uncommenting their `AppenderRef`s, shipped via the git-sourced server profile like everything else in `helm/server-profile/`. One field genuinely missing from the vendor's `server-log.json` JSON template vs. the required-fields list — `thread` — added directly; `trackingId`/`level`/`logger`/`message`/exception fields were already present.

**Entry criteria:** Phase 3b done (known Sim-A error line); Postgres reachable.

**Deliverables (done):**
- `helm/server-profile/instance/server/default/conf/log4j2.xml` — real vendor file, four targeted patches only (two `fileName`/`filePattern` fixes, two `AppenderRef`/`appender-ref` uncomments), everything else untouched.
- `helm/server-profile/instance/server/default/conf/log4j/json-templates/server-log.json` — added `thread` field.
- `helm/ping-devops/values.yaml` — `configMaps.fluent-bit-config` (inline `fluent-bit.conf` + `parsers.conf`: tails `server-json.log`/`admin-api-json.log`/`init.log`, `record_modifier` filter, `pgsql` output targeting `agent.pf_logs_raw`); `sidecars.pf-fluent-bit-admin`/`pf-fluent-bit-engine` (two near-identical entries, not one shared — `includeSidecars:` injects a container spec verbatim with no per-product override, so `PF_ROLE` forces two copies); per-product `out-dir`/`fluent-bit-state` `emptyDir` volumes + `includeVolumes: [fluent-bit-config]` + `includeSidecars:` on both `pingfederate-admin` and `pingfederate-engine`. Validated via `helm template` before touching the live cluster.
- `db/init.sql` — `agent.pf_logs_raw` (`id, tag, time, data jsonb, processed` — the columns Fluent Bit's plugin actually targets, pre-created with `id`/`processed` as extras since its own `CREATE TABLE IF NOT EXISTS` only ever references `tag`/`time`/`data` and never fights a richer pre-existing schema) plus `agent.pf_logs` (the normalized table Phase 5 populates — this one Fluent Bit never writes to at all).

**Exit criteria:** reproduce the Sim-A failure, confirm via `SELECT ... FROM agent.pf_logs_raw WHERE processed = false` that rows are landing with a correct `data` payload within seconds.

**Risk:** none Fluent-Bit-side now that signature computation moved out — the correctness-critical fingerprint-stability risk moves to Phase 5, where it belongs (easier to unit test in Python than in a Fluent Bit filter).

## Phase 5 — Postgres `agent` schema + detector (no LLM yet)

**Entry criteria:** Phase 4 done; Sim B (ImagePullBackOff via a bad `image.tag`) manually dry-run once to confirm its mechanics before wiring detection against it.

**Deliverables:**

- Extend `db/init.sql` with `agent.incidents` (status, fingerprint, service, resource, failure_class, first_seen, last_seen).
- `agent/detector.py` — two responsibilities, run as one poll loop:
  1. **Normalize pass**: `SELECT ... FROM agent.pf_logs_raw WHERE processed = false FOR UPDATE SKIP LOCKED` in small batches. Parse `tag` (e.g. `pf.server.admin`) into `log_type`/`pf_role`; redact secret-shaped values from `data`; extract `severity`/`logger`/`thread`/`message`/`exception_type`/`exception_message`/`stack_trace`/`tracking_id`/`namespace`/`pod_name` directly from `data`'s consistently-named JSON fields (direct field access, not fuzzy parsing — confirmed field names against the real `server-log.json` template in Phase 4); normalize IDs/timestamps/paths out of the message; compute `error_signature`; insert the normalized row into `agent.pf_logs` (`raw_log_id` references back to the source row); mark the `pf_logs_raw` row `processed = true`.
  2. **Detection pass**: aggregates `agent.pf_logs` (SQL `GROUP BY error_signature`) by repeating `error_signature` correlated with readiness failure (Sim A path), and watches the k8s API for pod state/warning events (Sim B path). Computes `fingerprint = sha256(service+namespace+resource+failure_class)`, dedups against open incidents, inserts `status=NEW` rows. Implements the 90s post-agent-upgrade quiet window to avoid self-triggering.
- First Python code in the repo — establish `pyproject.toml` + `uv` venv/`uv sync` here as the standard way to run Python for the rest of the project (system Python is an old 3.9.6; `uv` is already installed).

**Exit criteria:** trigger both sims by hand; confirm exactly one deduped `agent.incidents` row per trigger, and re-triggering the same unresolved fault doesn't create a second row. Additionally: verify the same underlying fault triggered twice produces an identical `error_signature` (no leaked timestamp/pod-name) — this is what the dedup check above depends on.

**Risk:** handle k8s watch-stream reconnects, not just the happy path — this runs continuously.

## Phase 6 — diagnostics + health MCP servers, evidence-bundle CLI

**Deliverables:** `mcp_servers/diagnostics/server.py` (read-only: `get_logs_window` — bounded excerpt only, never the whole stream — `get_pod_status`, `get_events`, `repo_diff` against a golden ref); `mcp_servers/health/server.py` (replica readiness, last Helm revision, health-probe check); `agent/dump_evidence.py` CLI that produces a JSON evidence bundle for an open incident. This bundle's schema becomes the eval fixture format in Phase 14 — decide it carefully now.

**Exit criteria:** CLI run against each sim's open incident produces a structured bundle with a relevant, bounded log excerpt or pod/event status plus a repo diff.

## Phase 7 — `agent_knowledge` (vector) + `config_baseline` (exact) tables + hybrid retrieval

Revised further, worked out in detail during a dedicated design pass (not just at Phase-7 time — `knowledge/golden-architecture.md` was drafted early, ahead of this phase, because Sim A work needed it sooner): agent-knowledge is not one table, it's two, doing two different jobs, populated and queried differently.

- **`agent.agent_knowledge`** (vector) — narrative/explanatory content only: invariants, traps, "why" (e.g. why `Recreate` strategy, the `SERVER_PROFILE_PATH`/`instance` trap, why the profile ships via git not a ConfigMap). Chunked one atomic fact per row (not per whole doc-section — a dense doc bundling many facts into one chunk dilutes retrieval precision), each row carrying a `related_keys text[]` column naming which `config_baseline` keys that fact is about. Schema: `id, content, content_tsv tsvector generated, content_vector vector(384), related_keys text[]`. No ANN index (ivfflat/HNSW) needed at this corpus size (a few dozen rows) — brute-force `ORDER BY content_vector <=> query` is instant and exact; add an index only if the corpus grows into the thousands.
- **`agent.config_baseline`** (plain SQL, no embedding) — the frozen, known-good exact values: `key, golden_value, source_file, captured_sha, verified_at`. Populated **once**, from the current already-verified-healthy `values.yaml` state, then **frozen** — it must never auto-resync on every push, or it could never disagree with a bad commit and drift detection becomes impossible by construction. It only changes via a deliberate re-baseline, gated by Phase 10's VERIFY step confirming a merged, approved change is the new known-good state (mirrors the README's Repair-PR-vs-Baseline-change-PR governance split — a repair PR fixes drift back to the existing baseline; a baseline-change PR deliberately promotes a new value, and only that second kind updates this table).

Why split instead of one table: embedding a key-value pair like `SERVER_PROFILE_PATH: helm/server-profile` and finding it via cosine similarity is strictly worse than `WHERE key = 'SERVER_PROFILE_PATH'` — exact-match config drift detection wants a deterministic diff, not an approximate nearest-neighbor guess. Vector search is for "I don't know the exact wording of what I'm looking for" (a symptom description); exact lookup is for "I know exactly which key I'm checking." The two tables are never queried by the same graph node either — see Phase 8's RETRIEVE/COMPARE split.

**Deliverables:** `knowledge/golden-architecture.md` (done — narrative-only, `###`-chunked, `related_keys` per chunk); `search/ingest.py` (parses frontmatter + `###` chunks, embeds each chunk locally, upserts into `agent.agent_knowledge`, content-hash-gated so unchanged chunks aren't re-embedded); `search/seed_baseline.py` (parses `helm/ping-devops/values.yaml` once, inserts the initial frozen rows into `agent.config_baseline` — a one-time seed script, not a continuous sync); `search/query.py` (Postgres full-text `ts_rank` over `content_tsv` + pgvector `<=>` cosine distance over `content_vector` on `agent_knowledge` only, merged via reciprocal rank fusion in Python).

**Exit criteria:** querying `agent_knowledge` with each sim's known symptom text surfaces the matching fact in the top 3 results, whose `related_keys` correctly names the `config_baseline` key(s) involved; `SELECT golden_value FROM agent.config_baseline WHERE key = 'pingfederate-admin.envs.SERVER_PROFILE_PATH'` returns the correct frozen value.

## Phase 8 — LangGraph diagnosis loop (read-only) — first demo milestone

**Deliverables:** `agent/schemas.py` (`Diagnosis` Pydantic model with `failure_class, root_cause, confidence, supporting_evidence_ids, needs_more_evidence`); `agent/llm.py` (provider-abstracted LLM client, env driven, targeting a hosted LLM API with a user-supplied key — not a local model — create root `.env.example` here, since only `infrastructure/.env.example` exists today); `agent/graph.py` + `agent/nodes/{triage,retrieve,compare}.py` implementing TRIAGE→RETRIEVE→COMPARE→DIAGNOSE, read-tools-only, bounded loop, single retry below confidence threshold then escalate.

Node/table routing is fixed by topology, not chosen by the LLM: RETRIEVE only ever queries `agent.agent_knowledge` (vector+keyword hybrid, symptom text in), which surfaces `related_keys`; COMPARE only ever queries `agent.config_baseline` (exact key match, using exactly those `related_keys`) plus the live ConfigMap/values.yaml for the current value, and diffs the two. Neither node does the other's job.

**Exit criteria:** trigger both sims for real, confirm the graph reaches DIAGNOSE with correct `failure_class`/`root_cause` and evidence IDs that trace back to real Phase-6 bundles. Worth a deliberate demo checkpoint here.

## Phase 9 — `repo_config` + `helm_ops` MCP servers, worktree patching (no push)

Revised: `repo_config`'s PR/merge half can lean on the existing GitHub MCP server instead of hand-built Gitea API calls — the project-specific part is still needed (the typed file-patching tools below are specific to this repo's config format, no MCP server does that), but create-PR/comment/merge become calls to a pre-built, maintained tool instead of custom code.

**Deliverables:** `agent/nodes/{diagnose,plan}.py` (PLAN emits typed actions + `depends_on`, no writes execute here); `mcp_servers/repo_config/server.py` (`patch_properties_key`, `patch_yaml_path`, both requiring `expected_file_sha256`, operating in a git worktree against the GitHub `iam-platform` remote, branch `remediation/inc-<id>`, returns a unified diff — nothing pushed); `mcp_servers/helm_ops/server.py` (`helm_render_validate`, `helm_upgrade_release`, `helm_rollback_release` — Python subprocessing `helm` directly with the same `helm upgrade --install pingfederate ...` shape `deploy-helm.sh` already runs for humans, just returning structured results — revision number, rendered diff, pass/fail — instead of a shell exit code). Revised: the *agent's own graph* only ever calls `helm_render_validate` here (a read-only dry-run, used to preview what a proposed patch would do, attached to the PR as a comment) — `helm_upgrade_release`/`helm_rollback_release` are built here but invoked only by the CI/CD pipeline in Phase 12, behind the scoped-token gate from Phase 11. The agent never redeploys PingFederate directly and holds no standing deploy credential. `policies/allowed_paths.yaml` + `policies/protected_keys.yaml` as defense-in-depth ahead of Phase 14's formal policy node.

**Exit criteria:** given a real Plan, the patch tool produces a correct diff, refuses on sha mismatch, worktree branch stays unpushed.

## Phase 10 — Approval → PR (agent's job ends here)

Revised (reconciliation pass): the agent no longer merges its own PR, deploys, or verifies. Its graph now ends once a PR exists. Merge, deploy, and verify move to Phase 12, gated by the token-issuance mechanism built in Phase 11 — the agent never holds a standing deploy credential or touches `main`/the live cluster directly.

**Entry criteria:** spot-check the GitHub MCP server's PR/merge tools work against the `iam-platform` repo early in this phase — it's load-bearing for everything else here.

**Deliverables:** `agent/nodes/{act,verify}.py`* + POLICY node; `agent.incidents` status-transition table (statuses: `DETECTED, DIAGNOSING, NEEDS_MORE_EVIDENCE, PR_CREATED, WAITING_FOR_PR_REVIEW, PR_REJECTED, DEPLOYING, VERIFYING, RESOLVED, FAILED` — the agent only ever writes up through `WAITING_FOR_PR_REVIEW`/`PR_REJECTED`; Phase 12's CI/CD pipeline owns the rest); `api/` FastAPI app (approve/reject endpoints for the PR-review decision itself, Postgres-backed, restart-safe); GitHub PR creation via the GitHub MCP server (commit, push, open PR with summary/evidence/diagnosis, post `helm_render_validate`'s dry-run output as a PR comment). Branch protection on `main` (no agent push, no self-approval, required human approval, required syntax/Helm-render checks) is configured here too, since it's what makes ending the agent's job at PR-creation actually safe.

*`agent/nodes/verify.py` here only validates the *proposal* pre-PR (syntax, render); the post-deploy VERIFY that checks rollout/health/error-signature-gone is Phase 12's, run by CI/CD, not the agent.

**Exit criteria:** a real incident produces a PR on GitHub with correct diff, evidence, and diagnosis attached, and the incident reaches `WAITING_FOR_PR_REVIEW`. No deploy has happened yet.

**Risk:** still the point where a wrong proposal becomes visible outside the agent (a PR is written to a real, shared repo) — POLICY node must reject protected-path/protected-key edits before this point, not rely on human review to catch it.

## Phase 11 — SPIFFE/SPIRE identity + local OAuth authorization service

New phase (reconciliation pass), inserted between the narrowed Phase 10 and the new Phase 12 it gates. Purpose: nothing may deploy PingFederate or run a dangerous runtime action without both (a) a SPIFFE-authenticated caller identity and (b) a scoped, single-use, human/approval-derived token bound to the exact operation — identity alone is not authorization.

**Entry criteria:** Phase 10 done (there's a real PR-merge event to gate deployment on).

**Deliverables:**

- SPIFFE/SPIRE server + agent registrations for each workload identity: `spiffe://self-healing-iam.local/{agent, git-mcp, ci-runner, authorization-service, helm-gateway, kubernetes-mcp, verifier}`. mTLS required between agent/tool services.
- `authorization/` FastAPI service (`app.py`, `token_issuer.py`, `token_validator.py`, `approvals.py`): `GET /.well-known/jwks.json`, `POST /oauth2/token`, `POST /webhooks/pull-request` (records an approved/merged PR event), `POST /approvals/direct-action` (records a human-approved dangerous runtime action, e.g. `k8s.rollout_restart`), `GET /approvals/{id}`.
- Deployment tokens: issued only after a recorded merge event, JWT claims bind `sub` (SPIFFE ID of the caller, e.g. `ci-runner`), `commit` (exact approved merge SHA), `release`, `namespace`, `scope: helm:upgrade`, single-use `jti`, short expiry (~5 min).
- Direct-action tokens: issued only after explicit human approval via the UI (Phase 13), bound to one exact tool + target + parameters, single-use.
- A "helm gateway" wrapping Phase 9's `helm_ops` MCP server tools, verifying both the SPIFFE mTLS identity and the token's claims (issuer/audience/expiry/scope/exact commit-release-namespace/unused `jti`) before calling `helm_upgrade_release`/`helm_rollback_release`.

**Exit criteria:** a token request without a valid SPIFFE identity is rejected; a token request without a recorded approval/merge event is rejected; a valid token successfully authorizes exactly one `helm_upgrade_release` call and is rejected on reuse.

**Risk:** most infrastructure-heavy phase relative to its payoff for a local demo — keep it scoped to what actually gates the two sims' deploy path, don't over-build unused identity types.

## Phase 12 — CI/CD: merge → deploy → verify → bounded rollback

New phase (reconciliation pass) — this is what Phase 10 used to do itself; now it's a separate, token-gated pipeline triggered by the PR merge from Phase 10, using Phase 11's authorization service and Phase 9's `helm_ops` tools.

**Entry criteria:** Phase 11 done (token issuance works); a merge-able PR exists from Phase 10.

**Deliverables:** GitHub Actions workflow (self-hosted or hosted runner, authenticated to the cluster via its SPIFFE identity) triggered on merge to `main`: requests a commit-bound token from the Phase 11 authorization service, calls the helm gateway's `helm_upgrade_release --wait --timeout --atomic` for the exact merged commit, then runs VERIFY (rollout complete, stability window, health probe, error signature gone from post-fix `agent.pf_logs`). On VERIFY failure: one-shot `helm_rollback_release` via the same gateway, then mark the incident `FAILED` and escalate — never loop. On success: mark `RESOLVED`, and (per Phase 7's baseline governance) if this PR was a deliberate baseline-change (not a repair), promote the new value into `agent.config_baseline` here — this is the only point that table is allowed to change.

**Exit criteria:** full flow succeeds end-to-end for both sims: PR merge → CI/CD requests token → deploy happens → VERIFY passes → incident `RESOLVED` with deployment/verification evidence recorded.

**Risk:** highest blast-radius phase — the first point anything actually redeploys the live cluster. Deliberately exercise the rollback path (force a bad VERIFY) and the token-rejection path (wrong commit, expired token, reused `jti`), not just the happy path.

## Phase 13 — UI (FastAPI + Jinja2 + HTMX + Bootstrap)

Revised: server-rendered UI, not a separate Vite/React frontend build — matches the reconciled spec's simpler stack and avoids a second build pipeline for a local demo.

**Entry criteria:** Phase 12's full pipeline already verified directly (curl/GitHub UI) — this phase is presentation over an already-correct backend.

**Deliverables:** `ui/app.py` + `ui/templates/` — Dashboard (incident ID, type, PF role, status, first/last seen, occurrence count, PR/deployment state); Incident detail (sanitized triggering logs, Kubernetes evidence, retrieved architecture facts + baseline values, diagnosis, exact patch, PR link, verification result); Direct-action approval page (exact tool/target/parameters/risk/reason/expiry, approve/reject — this is what issues Phase 11's direct-action tokens); Simulations page (clearly labeled test-injection buttons, isolated from remediation tooling).

**Exit criteria:** clicking a simulation button renders the full incident trace live through to `RESOLVED` with no manual API calls.

## Phase 14 — Policy tiers, action budget, resource locks, negative tests

**Deliverables:** `policies/risk_tiers.yaml`; POLICY node validating path allowlists, protected keys, value types, action budget, resource lock before AWAITING_APPROVAL; `tests/{unit,contract,e2e}` with explicit negative tests — path traversal, protected-key edit, command-injection-shaped input — all must be rejected at the tool/policy layer.

**Exit criteria:** negative test suite passes; each attack class is rejected with a clear error, never silently no-op'd or executed.

## Phase 15 — `incident_history` write-back + precedent retrieval

Revised: incident-history is a Postgres table (`agent.incident_history`, `narrative_vector vector(<dim>)`), same motivation as Phase 7 — one datastore instead of two. Kept as V1 scope (reconciliation pass) rather than deferred to V2.

**Deliverables:** `agent/history.py` (inserts into `agent.incident_history` once at CLOSE/ESCALATE, redacting secret-like values); `agent/nodes/retrieve.py` extended to query it filtered to `outcome='verified'`, ranked by `narrative_vector <=> :query_vector`, top-3, injected into the prompt explicitly labeled "precedent, not instruction."

**Exit criteria:** after closing 2+ same-`failure_class` incidents, a fresh incident's RETRIEVE step surfaces them as precedent.

## Phase 16 — Eval harness + baseline + README results table

Kept as V1 scope (reconciliation pass) rather than dropped.

**Deliverables:** `eval/fixtures/` (12 fixtures: 4 happy path, auth-failure→escalate, red-herring-diff, live-drift→reconcile-not-edit, two-faults-at-once, low-signal→request-more-then-escalate, 3 near-miss variants — built from Phase 6's evidence-bundle schema, replayed via a stub tool layer, no cluster needed); `eval/run_eval.py` (3 runs per fixture at low temperature; reports failure-class accuracy, target correctness, action correctness, escalation precision/recall, plan validity rate, mean tool calls/tokens/latency, mean+variance); `eval/baseline_rules.py` (deterministic mapper, expected to pass only the 4 happy-path fixtures); `eval/results.csv` + markdown table wired into `README.md`.

**Exit criteria:** `uv run eval/run_eval.py` completes in seconds, produces `eval/results.csv`, README table shows the baseline-vs-agent contrast the spec calls "the point."

## Cross-cutting

- Root `.env.example` doesn't exist yet — create it in Phase 8 when `LLM_PROVIDER`/API key/model first matter (hosted API, user-supplied key — not local Ollama), extend incrementally afterward (GitHub token if `iam-platform` ends up private, SPIRE/authorization-service settings in Phase 11, etc.) rather than front-loading it now.
- Establish `uv`-managed Python in Phase 5 (first Python code) and keep every later phase (`agent/`, `mcp_servers/`, `search/`, `api/`, `eval/`, `authorization/`) in that same environment.
- Stub a top-level `run-demo.sh` early (even as a placeholder) and fill it in as each piece lands, rather than writing "one command to start the demo" from scratch at the end.
- Simulation count stays at two (Sim A: `SERVER_PROFILE_PATH` trap; Sim B: `ImagePullBackOff` via bad `image.tag`) throughout — not the seven-scenario set from the reconciliation-pass spec, which are good V2 candidates but out of scope here.

## Critical files

- `infrastructure/main.tf` — cluster topology, namespace, secrets
- `helm/ping-devops/values.yaml` — PF profile-from-git wiring (Phase 3b) + Fluent Bit sidecar wiring (Phase 4)
- `helm/server-profile/instance/server/default/conf/log4j2.xml` — real vendor file, minimally patched to enable JSON logging (Phase 4)
- `helm/postgres.yaml` — raw manifest, the only non-Helm release in the stack
- `deploy-platform.sh` — Postgres (kubectl apply) bootstrap
- `push-to-github.sh` — separate, cluster-independent GitHub repo bootstrap
- `db/init.sql` — agent schema, grows through Phases 2, 4, 5, 7, 10, 15
- `knowledge/golden-architecture.md` — narrative RAG source, done ahead of Phase 7's ingest tooling
- `agent/detector.py` — raw-log normalize pass + dedup/fingerprint logic both sims depend on (Phase 4/5)
- `agent/dump_evidence.py` — evidence-bundle schema reused by eval fixtures
- `authorization/token_issuer.py` — the only place deployment/direct-action tokens are minted (Phase 11)

## Verification approach

Each phase above has its own exit criteria (a command to run or an observable cluster/index/API state) — there's no single end-to-end test until Phase 12, since this is infrastructure-first, agent-second work. The first true "does this work" milestone is Phase 8 (LangGraph reaches a correct diagnosis for both sims); the first "does this work end-to-end" milestone is Phase 12 (full merge→deploy→verify cycle, following the PR that Phase 10 already produced and Phase 11's token gate authorizes).
