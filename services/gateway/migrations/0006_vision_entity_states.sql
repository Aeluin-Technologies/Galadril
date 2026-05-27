-- Vision-owned physical table for entity states (mirrors platform/vision).

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS postgis CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_trgm CASCADE;

CREATE TABLE IF NOT EXISTS entity_states (
    tenant_id   TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    state_type  TEXT NOT NULL,
    state_value JSONB NOT NULL,
    geom        GEOMETRY(Point, 4326),
    event_time  TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

SELECT create_hypertable(
    'entity_states',
    'event_time',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

ALTER TABLE entity_states SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tenant_id, entity_id, state_type',
    timescaledb.compress_orderby = 'event_time DESC'
);

SELECT add_compression_policy('entity_states', INTERVAL '30 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_entity_states_tenant_entity_time
ON entity_states (tenant_id, entity_id, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_entity_states_geom
ON entity_states USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_entity_states_name_trgm
ON entity_states
USING GIN ((state_value->>'name') gin_trgm_ops);
