-- Non-destructive source-specific reputation migration.
--
-- Existing counters in proxies are deliberately retained for rollback. The
-- application seeds a pre-migration row into the configured default source
-- only when a proxy has no row in this table; it never copies one physical
-- proxy's legacy history into every source.

CREATE TABLE IF NOT EXISTS proxy_source_reputation (
    proxy_id INT NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
    source_name VARCHAR(50) NOT NULL,
    success_count INT NOT NULL DEFAULT 0,
    failure_count INT NOT NULL DEFAULT 0,
    last_feedback_ts TIMESTAMPTZ,
    quality_slow DOUBLE PRECISION NOT NULL,
    quality_fast DOUBLE PRECISION NOT NULL,
    quality_updated_ts TIMESTAMPTZ,
    recent_results JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (proxy_id, source_name)
);

CREATE INDEX IF NOT EXISTS idx_proxy_source_reputation_source
    ON proxy_source_reputation (source_name, proxy_id);

COMMENT ON TABLE proxy_source_reputation IS 'Durable online-learning state isolated by physical proxy and request source.';
