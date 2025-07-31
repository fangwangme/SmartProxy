-- This script sets up the required table for the Intelligent Proxy Service.
-- It should be run once on your PostgreSQL database.

-- Drop the table if it exists to ensure a clean setup.
DROP TABLE IF EXISTS proxies CASCADE;

-- Table to store the physical attributes and validation status of each proxy.
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

-- Grant permissions to your application's user if necessary.
-- Example: GRANT ALL ON proxies TO my_proxy_user;
-- Example: GRANT USAGE, SELECT ON SEQUENCE proxies_id_seq TO my_proxy_user;

COMMENT ON TABLE proxies IS 'Stores physical attributes and validation status of all discovered proxies.';
COMMENT ON COLUMN proxies.is_active IS 'True if the proxy passed the last validation, false otherwise.';
