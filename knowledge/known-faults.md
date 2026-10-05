---
doc_id: known-faults-pingfederate
doc_type: known-faults
title: Known Faults — PingFederate Admin/Engine
service: [pingfederate-admin, pingfederate-engine]
source_paths:
  - helm/ping-devops/values.yaml
  - helm/server-profile/instance/bulk-config/data.json.subst
---

<!--
Symptom-to-cause entries for faults this deployment has actually hit,
chunked one fault per ### heading for embedding. golden-architecture.md
describes what correct looks like and why; this file describes what a
specific failure looks like in the logs and where its cause lives.
Each entry's `related_keys:` line names the config_baseline keys the
agent should look up next, same convention as golden-architecture.md.
-->

### JDBC datastore failure: pods stay Ready but the datastore cannot connect
related_keys: [pingfederate-admin.envs.POSTGRES_JDBC_URL, pingfederate-engine.envs.POSTGRES_JDBC_URL]

If `POSTGRES_JDBC_URL` points at an unreachable host, PingFederate still starts and both pods report Ready — its JDBC datastore check does not block readiness. The only signal is a repeated ERROR from logger `org.sourceid.saml20.domain.mgmt.impl.DataSourceManagerImpl`: "Unable to load custom data source instance: JDBC-<id>", where the datastore is `PostgresPfApp`. That datastore's `connectionUrl` in `data.json.subst` is the placeholder `${POSTGRES_JDBC_URL}`. The value is defined once, in `values.yaml` under each product's `envs:` block (`pingfederate-admin.envs` and `pingfederate-engine.envs`); `data.json.subst` only references it. A corrected value belongs in `values.yaml`, not in `data.json.subst`. Reproduced live as Sim A.
