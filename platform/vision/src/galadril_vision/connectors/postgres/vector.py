from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import orjson
import structlog
from pgvector.psycopg import register_vector_async
from psycopg import sql

from galadril_vision.common.exceptions import VectorSearchError
from galadril_vision.common.types import EntityEmbedding, EmbeddingModality

if TYPE_CHECKING:
    from galadril_vision.common.config import PostgresConfig
    from galadril_vision.connectors.postgres.client import PostgresClient

logger = structlog.get_logger(__name__)

_SQL_CREATE_EXTENSIONS = """
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS vector CASCADE;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
"""

_SQL_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS entity_embeddings (
    id TEXT,
    entity_id TEXT NOT NULL,
    modality TEXT NOT NULL,
    embedding vector({dimensions}),
    tenant_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, created_at)
);
"""

_SQL_CREATE_HYPERTABLE = """
SELECT create_hypertable(
    'entity_embeddings',
    'created_at',
    if_not_exists => TRUE,
    migrate_data => TRUE
);
"""

_SQL_CONFIGURE_COMPRESSION = """
ALTER TABLE entity_embeddings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tenant_id, modality, entity_id',
    timescaledb.compress_orderby = 'created_at DESC'
);
SELECT add_compression_policy('entity_embeddings', INTERVAL '30 days', if_not_exists => TRUE);
"""

_SQL_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_entity_embeddings
ON entity_embeddings
USING diskann (embedding);

CREATE INDEX IF NOT EXISTS idx_entity_embeddings_tenant_time
ON entity_embeddings (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_entity_embeddings_tenant_entity_time
ON entity_embeddings (tenant_id, entity_id, created_at DESC);
"""


class VectorStore:
    """Unified embedding storage and similarity search using pgvectorscale."""

    def __init__(self, client: PostgresClient, config: PostgresConfig) -> None:
        self._client = client
        self._config = config

    async def initialize(self) -> None:
        """Create the multimodal embeddings table and index."""
        async with self._client.connection() as conn:
            await register_vector_async(conn)
            await conn.execute(_SQL_CREATE_EXTENSIONS)

            query_table = sql.SQL(_SQL_CREATE_TABLE).format(
                dimensions=sql.Literal(self._config.vector_dimensions)
            )
            await conn.execute(query_table)
            await conn.execute(_SQL_CREATE_HYPERTABLE)
            await conn.execute(_SQL_CONFIGURE_COMPRESSION)
            await conn.execute(_SQL_CREATE_INDEXES)

        logger.info(
            "vector_store_initialized",
            dimensions=self._config.vector_dimensions,
        )

    async def find_similar(
        self,
        embedding: list[float],
        modality: EmbeddingModality,
        tenant_id: str,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """Find similar embeddings using vectorscale with strict tenant isolation."""
        async with self._client.connection() as conn:
            await register_vector_async(conn)

            query = sql.SQL("""
                SELECT entity_id, similarity
                FROM (
                    SELECT
                        entity_id,
                        1 - (embedding <=> $1::vector) AS similarity,
                        embedding <=> $1::vector AS distance
                    FROM entity_embeddings
                    WHERE modality = $2 AND tenant_id = $5
                    ORDER BY distance
                    LIMIT $4
                ) AS sub
                WHERE similarity >= $3;
            """)

            async with conn.cursor() as cur:
                await cur.execute(
                    query,
                    (
                        embedding,
                        modality.value,
                        self._config.similarity_threshold,
                        top_k,
                        tenant_id,
                    ),
                )
                rows = await cur.fetchall()

            return [(str(row[0]), float(row[1])) for row in rows]

    async def resolve_entity(self, record: EntityEmbedding) -> EntityEmbedding:
        if not record.vector:
            return record
        try:
            matches = await self.find_similar(
                record.vector, record.modality, record.tenant_id, top_k=1
            )
            if matches:
                entity_id, confidence = matches[0]
                record.entity_id = entity_id
                record.confidence = confidence
                record.is_unknown = False
                logger.debug(
                    "entity_resolved",
                    modality=record.modality,
                    entity_id=entity_id,
                )
            else:
                record.is_unknown = True
        except Exception as exc:
            raise VectorSearchError(f"Entity resolution failed: {exc}") from exc
        return record

    async def store_embedding(
        self, record: EntityEmbedding, entity_id: str
    ) -> None:
        if not record.tenant_id:
            raise PermissionError(f"tenant_id is missing for {entity_id}")

        async with self._client.connection() as conn:
            await register_vector_async(conn)
            query = sql.SQL("""
                INSERT INTO entity_embeddings (id, entity_id, modality, embedding, tenant_id, metadata, created_at)
                VALUES ($1, $2, $3, $4::vector, $5, $6, $7)
            """)
            metadata_json = orjson.dumps(record.metadata).decode()
            await conn.execute(
                query,
                (
                    record.embedding_id,
                    entity_id,
                    record.modality.value,
                    record.vector,
                    record.tenant_id,
                    metadata_json,
                    datetime.now(timezone.utc),
                ),
            )

    async def store_embeddings_batch(
        self, records: list[tuple[EntityEmbedding, str]]
    ) -> None:
        """Store multiple embeddings in a single batch insert."""
        if not records:
            return

        params = []
        now = datetime.now(timezone.utc)
        for record, entity_id in records:
            if not record.tenant_id:
                raise PermissionError(f"tenant_id is missing for {entity_id}")

            metadata_json = orjson.dumps(record.metadata).decode()
            params.append(
                (
                    record.embedding_id,
                    entity_id,
                    record.modality.value,
                    record.vector,
                    record.tenant_id,
                    metadata_json,
                    now,
                )
            )

        async with self._client.connection() as conn:
            await register_vector_async(conn)
            query = sql.SQL("""
                INSERT INTO entity_embeddings (id, entity_id, modality, embedding, tenant_id, metadata, created_at)
                VALUES ($1, $2, $3, $4::vector, $5, $6, $7)
            """)
            async with conn.cursor() as cur:
                await cur.executemany(query, params)

        logger.debug("embeddings_batch_inserted", count=len(records))
