-- This script sets up the required tables for the Intelligent Proxy Service.
-- It should be run once on your PostgreSQL database.

-- Drop the tables if they exist to ensure a clean setup.
DROP TABLE IF EXISTS source_stats_by_minute;
DROP TABLE IF EXISTS proxies CASCADE;

-- =================================================================
-- Table to store the physical attributes and validation status of each proxy.
-- =================================================================
CREATE TABLE proxies (
    -- Unique identifier for the proxy record.
    id SERIAL PRIMARY KEY,
    
    -- The protocol of the proxy (e.g., 'http', 'socks5').
    protocol VARCHAR(10) NOT NULL,
    
    -- The IP address of the proxy. Indexed for fast lookups.
    ip VARCHAR(45) NOT NULL,
    
    -- The port number of the proxy.
    port INT NOT NULL,
    
    -- The last measured latency in milliseconds. Null if never successfully validated.
    latency_ms INT,
    
    -- The anonymity level determined during validation (e.g., 'elite', 'transparent').
    anonymity_level VARCHAR(20),
    
    -- The geographical country of the proxy. Can be populated by an IP lookup service.
    country VARCHAR(50),
    
    -- A boolean flag indicating if the proxy is currently considered alive and usable.
    -- This is the primary flag for selecting proxies for business use. Indexed.
    is_active BOOLEAN DEFAULT false,
    
    -- Timestamp of when the proxy was first added to the database.
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Timestamp of the last time this proxy was successfully or unsuccessfully validated. Indexed.
    last_validated_at TIMESTAMPTZ,
    
    -- Ensures that each proxy (protocol, ip, port combination) is unique in the table.
    UNIQUE (protocol, ip, port)
);

-- Create indexes for faster queries on critical columns.
CREATE INDEX idx_proxies_is_active ON proxies (is_active);
CREATE INDEX idx_proxies_last_validated_at ON proxies (last_validated_at);

COMMENT ON TABLE proxies IS 'Stores physical attributes and validation status of all discovered proxies.';
COMMENT ON COLUMN proxies.is_active IS 'True if the proxy passed the last validation, false otherwise.';


-- =================================================================
-- [NEW] Table to store aggregated minute-level feedback statistics.
-- =================================================================
CREATE TABLE source_stats_by_minute (
    -- The timestamp truncated to the minute. Part of the composite primary key.
    minute TIMESTAMPTZ NOT NULL,
    
    -- The name of the target source (e.g., 'insolvencydirect'). Part of the composite primary key.
    source_name VARCHAR(50) NOT NULL,
    
    -- The total number of successful requests in this minute.
    success_count INT NOT NULL DEFAULT 0,
    
    -- The total number of failed requests in this minute.
    failure_count INT NOT NULL DEFAULT 0,
    
    -- Composite primary key to ensure each source has only one entry per minute.
    PRIMARY KEY (minute, source_name)
);

-- Create an index on the timestamp for efficient date-based lookups.
CREATE INDEX idx_source_stats_minute ON source_stats_by_minute (minute);

COMMENT ON TABLE source_stats_by_minute IS 'Stores aggregated success/failure counts for each source per minute.';
COMMENT ON COLUMN source_stats_by_minute.minute IS 'Timestamp truncated to the minute, e.g., 2025-08-02 16:30:00';

-- Grant permissions to your application's user if necessary.
-- Example: GRANT ALL ON ALL TABLES IN SCHEMA public TO my_proxy_user;
-- Example: GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO my_proxy_user;
