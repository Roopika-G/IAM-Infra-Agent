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

-- No separate normalized "pf_logs" table — deliberately cut (see plan.md
-- Phase 5). Extraction/signature computation happens transiently in Python
-- (agent/detector.py) against pf_logs_raw directly; only the *result*
-- (a fingerprint + one representative sample) gets persisted, on
-- agent.incidents below. Nothing pre-fetched or pre-bundled beyond that —
-- the live agent (Phase 8) pulls any further context it needs itself via
-- MCP tools, on demand, rather than from a pre-built copy of everything.

-- ingest_cursor: single-row bookmark so detector.py never rescans
-- pf_logs_raw from the beginning. The `id boolean primary key default
-- true check (id)` trick is a standard Postgres way to enforce exactly one
-- row. Advanced in the same transaction as the incidents it produces (see
-- detector.py) so a crash mid-batch can never skip unprocessed rows.
CREATE TABLE IF NOT EXISTS agent.ingest_cursor (
    id             BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    last_seen_time TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT 'epoch'
);
INSERT INTO agent.ingest_cursor (id) VALUES (true) ON CONFLICT DO NOTHING;

-- incidents: one row per deduplicated fault. The partial unique index is
-- what actually enforces "one open incident per fingerprint" — dedup logic
-- lives in the database constraint, not just application code, so a bug in
-- detector.py can't silently create duplicates.
CREATE TABLE IF NOT EXISTS agent.incidents (
    id               BIGSERIAL PRIMARY KEY,
    fingerprint      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'NEW',
    log_type         TEXT,             -- from pf_logs_raw.tag, e.g. 'server', 'init'
    pf_role          TEXT,             -- from pf_logs_raw.tag, e.g. 'admin', 'engine'
    logger           TEXT,
    exception_type   TEXT,
    sample_message   TEXT,             -- one representative full message, not truncated
    first_seen       TIMESTAMPTZ NOT NULL,
    last_seen        TIMESTAMPTZ NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS incidents_open_fingerprint_idx
    ON agent.incidents (fingerprint) WHERE status NOT IN ('RESOLVED', 'FAILED');
