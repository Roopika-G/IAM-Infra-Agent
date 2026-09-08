---
doc_id: golden-architecture-pingfederate
doc_type: architecture
title: Golden Architecture — PingFederate Admin/Engine
service: [pingfederate-admin, pingfederate-engine]
source_paths:
  - helm/ping-devops/values.yaml
  - helm/server-profile/instance/bulk-config/data.json.subst
  - helm/server-profile/instance/bin/run.properties.subst
  - data.json
  - infrastructure/main.tf
  - deploy-helm.sh
---

<!--
This file holds narrative/explanatory knowledge only — the "why" behind this
deployment's config, chunked one atomic fact per ### heading for embedding.
It does NOT hold exact current key/value pairs (SERVER_PROFILE_PATH's actual
value, port numbers, resource limits, etc.) — those live in the plain SQL
table agent.config_baseline, looked up by exact key match, not by
similarity search. Each ### entry below carries a `related_keys:` line
naming which config_baseline keys it's about, so the agent's COMPARE step
knows exactly what to look up after RETRIEVE finds this entry.
-->

### jwk and license are an immutable matched pair
related_keys: []

`pf.jwk` (master key) and `pingfederate.lic` (license) must never change independently of each other. `pf.jwk` decrypts the `systemKeys` and encrypted secrets already baked into the deployed bulk-config — swapping either file without the other breaks decryption of everything encrypted under the old key. Both are sourced once from local files, gitignored, never committed.

### PF startup requires license, password, and profile sync all at once
related_keys: []

PingFederate only starts successfully when all of these hold simultaneously: the license secret is mounted at the exact expected paths inside the pod, `PING_IDENTITY_PASSWORD` and `POSTGRES_JDBC_PASSWORD` are injected from their secrets, and the git-sourced server profile clones and merges into staging before PingFederate reads any config. If the profile merge silently no-ops, the pod still starts and reports Ready — but against the wrong config. That is a drift failure, not a crash, and is harder to detect than an outright startup failure.

### data.json.subst structural blocks are one-time bootstrap state, not editable config
related_keys: []

Blocks like `keyPairs/sslServer` (cert blobs), `administrativeAccounts`, `serverSettings/systemKeys`, and `license/agreement` inside `data.json.subst` must never be hand-edited or regenerated. Regenerating them (e.g. re-exporting bulk-config from a fresh instance) can invalidate the license binding tied to the current `pf.jwk`. Only the `${...}` placeholder values are meant to change, and only alongside a matching `values.yaml` `envs:` update.

### The SERVER_PROFILE_PATH /instance trap
related_keys: [pingfederate-admin.envs.SERVER_PROFILE_PATH, pingfederate-engine.envs.SERVER_PROFILE_PATH]

Appending `/instance` to `SERVER_PROFILE_PATH` looks like a natural correction — it matches the real on-disk directory layout — but the startup hook already appends a literal `instance` subdirectory itself when resolving this path. Pointing past it makes the git-sourced profile merge silently skip. The pod still starts and reports Ready, but runs against a stale or default profile instead of the one in git. This is a strong candidate for a hand-verified Sim A fault: it produces drift rather than a crash.

### data.json vs data.json.subst
related_keys: []

`data.json` at the repo root is a raw, fully-resolved bulk-config export pulled from a running instance for reference only — it is never deployed anywhere and PingFederate never reads it directly. `helm/server-profile/instance/bulk-config/data.json.subst` is the file actually deployed via the git-sourced profile mechanism. It is the same structure as `data.json`, with one deliberate addition: a second JDBC datastore (`PostgresPfApp`) whose connection URL, username, and password are `${...}` placeholders instead of literal values.

### .subst means substitution template, not final config
related_keys: [pingfederate-admin.envs.POSTGRES_JDBC_URL, pingfederate-admin.envs.POSTGRES_JDBC_USERNAME]

The `${...}` placeholders inside `.subst` files are resolved at container startup from the pod's environment, which comes from `values.yaml`'s per-product `envs:` block and secret-backed env vars. It is safe to add or remove unrelated config inside a `.subst` file. It is not safe to rename or remove an existing placeholder without updating the matching `values.yaml` entry — the result is a literal, unresolved `${...}` string landing in PingFederate's live config, which fails to parse or connect.

### Why the engine uses Recreate instead of RollingUpdate
related_keys: []

An old engine pod still alive during an admin restart can become the cluster coordinator and cause the admin's bulk-import replication step to fail with `403 license_agreement_not_accepted`. The engine's deployment strategy is deliberately `Recreate` (stop-then-start) rather than `RollingUpdate` to avoid this overlap window.

### Why the server profile is delivered via git instead of a ConfigMap
related_keys: []

The server profile directory includes a binary JDBC driver jar (`postgresql-42.7.9.jar`). Kubernetes ConfigMaps have a hard 1MiB size limit and are not meant to hold binaries at all. Git has neither constraint, so the profile is git-cloned fresh into the pod at every startup instead of being mounted as a ConfigMap.

### A profile change pushed to git doesn't reach an already-running pod
related_keys: []

The container only clones the server profile at its own startup. Pushing a profile change to the repo does nothing to a pod that's already running, and if nothing in `values.yaml` changed either, a plain `helm upgrade` is a no-op that restarts nothing. `deploy-helm.sh` always runs `kubectl rollout restart deployment` immediately after `helm upgrade --install`, specifically so a profile-only edit actually takes effect.

### Two unrelated ConfigMap mechanisms in this chart
related_keys: []

`{product}.envs:` (e.g. `pingfederate-admin.envs`) is the mechanism actually used in this deployment — per-product values get rendered into a ConfigMap consumed via `envFrom`. The separate top-level `configMaps:` key is a generic passthrough that renders arbitrary ConfigMap resources 1:1 and is currently unused (empty) in this deployment. These two are unrelated code paths and should not be assumed to interact with the `${...}` substitution mechanism the same way.

### Some run.properties.subst placeholders are chart defaults, not repo-authored
related_keys: []

Placeholders like `PF_ADMIN_PUBLIC_HOSTNAME`, `PF_CONSOLE_TITLE`, `OPERATIONAL_MODE`, and `CLUSTER_BIND_ADDRESS` are not set anywhere in this repo's `values.yaml` — they come from the vendored chart's own helper template (`templates/pinglib/_env-vars.tpl`), computed from ingress/gateway settings. Their exact resolved values must be confirmed against the live rendered ConfigMap in-cluster, not assumed from this document.

### Never auto-patch these without human approval
related_keys: []

`pf.jwk` or `pingfederate.lic` file contents, `PING_IDENTITY_PASSWORD`, `POSTGRES_JDBC_PASSWORD`, and `data.json.subst`'s cert/systemKeys/administrativeAccounts/license-agreement blocks. Any proposed remediation touching these must escalate for human approval regardless of confidence — never auto-apply.
