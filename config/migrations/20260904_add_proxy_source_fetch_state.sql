-- Retain bounded proxy-source backoff across process restarts.
CREATE TABLE IF NOT EXISTS proxy_source_fetch_state (
    source_name VARCHAR(100) PRIMARY KEY,
    failure_count INT NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    failure_class VARCHAR(20) NOT NULL
);

COMMENT ON TABLE proxy_source_fetch_state IS 'Bounded source-fetch backoff retained across process restarts.';
