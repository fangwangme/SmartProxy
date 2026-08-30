-- Non-destructive upgrade for existing SmartProxy databases.
--
-- CREATE INDEX CONCURRENTLY cannot run inside a transaction block. Execute this
-- file directly with psql; it keeps source_stats_by_minute available for reads
-- and writes while PostgreSQL builds the index.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_source_stats_by_minute_source_minute
    ON source_stats_by_minute (source_name, minute);
