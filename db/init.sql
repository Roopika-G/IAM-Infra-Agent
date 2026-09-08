-- Applied every time by deploy-platform.sh right after Postgres comes up
-- (idempotent — safe to rerun on an existing database).
--
-- vector: pgvector, for agent-knowledge/incident-history embeddings
-- (Phases 7/13) — this replaces what would otherwise be OpenSearch indices.
CREATE EXTENSION IF NOT EXISTS vector;

-- pf_app: PingFederate's own JDBC datastore (wired up in a later phase).
-- agent:  incident state/audit tables, populated starting Phase 5. Also
--         holds pf_logs (Phase 4, replacing OpenSearch's pf-logs-*) and the
--         agent-knowledge/incident-history tables (Phases 7/13).
CREATE SCHEMA IF NOT EXISTS pf_app;
CREATE SCHEMA IF NOT EXISTS agent;

-- pf_logs_raw: written directly by the Fluent Bit sidecars' pgsql output
-- plugin (helm/ping-devops/values.yaml's fluent-bit-config ConfigMap).
-- Confirmed against the plugin's real behavior, not assumed: it always
-- creates/writes a fixed tag/time/data(jsonb) shape and cannot target
-- arbitrary custom columns — record_modifier fields (pf_role, namespace,
-- pod_name) end up INSIDE `data`, not as separate SQL columns. id and
-- processed are ours, added on top; Fluent Bit's own `CREATE TABLE IF NOT
-- EXISTS` only ever references tag/time/data, so pre-creating with extra
-- columns (with defaults) here is safe and never fought over.
CREATE TABLE IF NOT EXISTS agent.pf_logs_raw (
    id        BIGSERIAL PRIMARY KEY,
    tag       TEXT,
    time      TIMESTAMP WITHOUT TIME ZONE,
    data      JSONB,
    processed BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS pf_logs_raw_unprocessed_idx
    ON agent.pf_logs_raw (id) WHERE NOT processed;

-- pf_logs: normalized output of Phase 5's detector.py, never written by
-- Fluent Bit directly (see plan.md Phase 4/5 — Fluent Bit's pgsql output
-- can't do hashing/regex extraction reliably, so that work happens here in
-- Python against pf_logs_raw instead).
CREATE TABLE IF NOT EXISTS agent.pf_logs (
    id               BIGSERIAL PRIMARY KEY,
    raw_log_id       BIGINT REFERENCES agent.pf_logs_raw(id),
    event_time       TIMESTAMPTZ NOT NULL,
    log_type         TEXT NOT NULL,   -- derived from pf_logs_raw.tag, e.g. 'server', 'admin_api', 'init'
    pf_role          TEXT NOT NULL,   -- derived from pf_logs_raw.tag, e.g. 'admin', 'engine'
    namespace        TEXT,
    pod_name         TEXT,
    severity         TEXT,
    logger           TEXT,
    thread           TEXT,
    message          TEXT,
    exception_type   TEXT,
    exception_message TEXT,
    stack_trace      TEXT,
    tracking_id      TEXT,
    error_signature  TEXT
);
CREATE INDEX IF NOT EXISTS pf_logs_error_signature_idx ON agent.pf_logs (error_signature);
CREATE INDEX IF NOT EXISTS pf_logs_event_time_idx ON agent.pf_logs (event_time);
