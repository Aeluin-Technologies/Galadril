-- Vision-owned physical table (mirrors platform/vision).

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS vector CASCADE;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;

CREATE TABLE IF NOT EXISTS entity_embeddings (
    id TEXT,
    entity_id TEXT NOT NULL,
    modality TEXT NOT NULL,
    embedding vector(1024),
    tenant_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, created_at)
);

SELECT create_hypertable(
    'entity_embeddings',
    'created_at',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

ALTER TABLE entity_embeddings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tenant_id, modality, entity_id',
    timescaledb.compress_orderby = 'created_at DESC'
);

SELECT add_compression_policy('entity_embeddings', INTERVAL '30 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_entity_embeddings
ON entity_embeddings
USING diskann (embedding);

CREATE INDEX IF NOT EXISTS idx_entity_embeddings_tenant_time
ON entity_embeddings (tenant_id, created_at DESC)

CREATE INDEX IF NOT EXISTS idx_entity_embeddings_tenant_entity_time
ON entity_embeddings (tenant_id, entity_id, created_at DESC)
