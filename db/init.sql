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
