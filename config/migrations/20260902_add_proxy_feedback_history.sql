-- Non-destructive upgrade for existing SmartProxy databases.
--
-- Adds the durable half of proxy reputation. The in-memory stats pool caps how
-- much history it retains, so a proxy that failed its way out of the pool and
-- later passed validation again was re-seeded as a pristine candidate: eviction
-- was an amnesty. These columns let _sync_and_select_top_proxies() re-seed such
-- a proxy with the record it actually earned.
--
-- Safe to run against a live database: the columns are added with defaults that
-- PostgreSQL 11+ fills in without rewriting the table, and nothing reads them
-- until the new code is deployed.

ALTER TABLE proxies
    ADD COLUMN IF NOT EXISTS feedback_success_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS feedback_failure_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS feedback_last_ts TIMESTAMPTZ;

COMMENT ON COLUMN proxies.feedback_success_count IS 'Durable copy of the in-memory success counter, so eviction from the stats pool is not reputation loss.';
COMMENT ON COLUMN proxies.feedback_failure_count IS 'Durable copy of the in-memory failure counter, restored when a proxy is re-seeded into the stats pool.';
COMMENT ON COLUMN proxies.feedback_last_ts IS 'Timestamp of the newest feedback behind those counters; drives time decay on a restored record.';
