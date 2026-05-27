-- Timescale constraint: UNIQUE/PK must include partition column (event_time).

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

CREATE TABLE IF NOT EXISTS eskg_events (
    event_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (event_id, event_time)
);

SELECT create_hypertable(
    'eskg_events',
    'event_time',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

ALTER TABLE eskg_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tenant_id, event_type', 
    timescaledb.compress_orderby = 'event_time DESC'
);

SELECT add_compression_policy('eskg_events', INTERVAL '30 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_eskg_events_tenant_type_time
ON eskg_events (tenant_id, event_type, event_time DESC);
