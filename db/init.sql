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
-- Deliberately in the public schema, NOT agent — confirmed live (not
-- assumed) that the plugin's `Table` config value is never split on ".",
-- it's wrapped in one pair of double quotes as a single literal
-- identifier. A `Table agent.pf_logs_raw` config value therefore does NOT
-- target schema `agent` table `pf_logs_raw` — it creates/targets a table
-- literally NAMED "agent.pf_logs_raw" (dot and all) in whatever schema is
-- first in the connecting role's search_path (public, by default). Only a
-- bare, unqualified name here actually works.
--
-- Exactly 3 columns, nothing more: the plugin's own generated INSERT is
-- positional with no explicit column list (`INSERT INTO t SELECT tag,
-- time, data FROM ...`), hard-coded to those 3 values. Adding id/processed
-- columns — the original plan here — silently breaks every future insert
-- with a column-count mismatch. Confirmed by capturing the live query in
-- pg_stat_activity before assuming otherwise.
CREATE TABLE IF NOT EXISTS public.pf_logs_raw (
    tag  TEXT,
    time TIMESTAMP WITHOUT TIME ZONE,
    data JSONB
);

-- pf_logs: normalized output of Phase 5's detector.py, never written by
-- Fluent Bit directly (see plan.md Phase 4/5 — Fluent Bit's pgsql output
-- can't do hashing/regex extraction reliably, so that work happens here in
-- Python against pf_logs_raw instead). No FK back to pf_logs_raw (it has
-- no id column to reference — see above); Phase 5 tracks ingest progress
-- via a time high-water-mark instead of a processed flag.
CREATE TABLE IF NOT EXISTS agent.pf_logs (
    id               BIGSERIAL PRIMARY KEY,
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
