from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import orjson
import structlog
from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection, sql

from galadril_vision.common.exceptions import (
    TenantIsolationError,
    VectorSearchError,
)
from galadril_vision.common.types import (
    EmbeddingModality,
    EntityEmbedding,
    normalize_tenant_id,
    require_same_tenant,
)

if TYPE_CHECKING:
    from galadril_vision.common.config import PostgresConnectorConfig
    from galadril_vision.connectors.postgres.client import PostgresClient

logger = structlog.get_logger(__name__)


class VectorStore:
    """Unified embedding storage and similarity search using pgvectorscale."""

    def __init__(
        self, client: PostgresClient, config: PostgresConnectorConfig
    ) -> None:
        self._client = client
        self._config = config

    def _validate_embedding(self, embedding: list[float]) -> list[float]:
        if not embedding:
            raise VectorSearchError("embedding vector is empty")
        if len(embedding) != int(self._config.vector_dimensions):
            raise VectorSearchError(
                "embedding dimension mismatch: "
                f"expected {self._config.vector_dimensions}, got {len(embedding)}"
            )
        return embedding

    def _embedding_params(
        self,
        record: EntityEmbedding,
        entity_id: str,
        *,
        expected_tenant_id: str,
        created_at: datetime,
    ) -> tuple[str, str, str, list[float], str, str, datetime]:
        tenant_id = require_same_tenant(expected_tenant_id, record.tenant_id)
        metadata = record.metadata.copy()
        if "tenant_id" in metadata:
            require_same_tenant(tenant_id, metadata["tenant_id"])
        metadata["tenant_id"] = tenant_id
        if not entity_id:
            raise VectorSearchError(
                "entity_id is required for embedding insert"
            )
        return (
            record.embedding_id,
            entity_id,
            record.modality.value,
            self._validate_embedding(record.vector),
            tenant_id,
            orjson.dumps(metadata).decode(),
            created_at,
        )

    async def initialize(self) -> None:
        """Registers the pgvector adapter in the connection pool."""
        async with self._client.connection() as conn:
            await register_vector_async(conn)

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
        min_similarity: float | None = None,
    ) -> list[tuple[str, float]]:
        """Find similar embeddings using vectorscale with strict tenant isolation."""
        tenant_id_val = normalize_tenant_id(tenant_id)
        embedding_val = self._validate_embedding(embedding)
        min_similarity_val = (
            self._config.similarity_threshold
            if min_similarity is None
            else float(min_similarity)
        )
        if top_k < 1:
            raise VectorSearchError("top_k must be at least 1")

        async with self._client.connection() as conn:
            await register_vector_async(conn)

            query = sql.SQL("""
                SELECT entity_id, similarity, tenant_id
                FROM (
                    SELECT
                        entity_id,
                        1 - (embedding <=> %s::vector) AS similarity,
                        embedding <=> %s::vector AS distance,
                        tenant_id
                    FROM entity_embeddings
                    WHERE modality = %s AND tenant_id = %s
                    ORDER BY distance
                    LIMIT %s
                ) AS sub
                WHERE similarity >= %s;
            """)

            async with conn.cursor() as cur:
                await cur.execute(
                    query,
                    (
                        embedding_val,
                        embedding_val,
                        modality.value,
                        tenant_id_val,
                        top_k,
                        min_similarity_val,
                    ),
                )
                rows = await cur.fetchall()

            results: list[tuple[str, float]] = []
            for row in rows:
                returned_tenant = normalize_tenant_id(str(row[2]))
                if returned_tenant != tenant_id_val:
                    raise TenantIsolationError(
                        "vector search returned a different tenant",
                        tenant_id=returned_tenant,
                    )
                results.append((str(row[0]), float(row[1])))
            return results

    async def resolve_entity(self, record: EntityEmbedding) -> EntityEmbedding:
        if not record.vector:
            return record
        tenant_id = normalize_tenant_id(record.tenant_id)
        try:
            matches = await self.find_similar(
                record.vector, record.modality, tenant_id, top_k=1
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

    async def store_embedding_on_connection(
        self,
        conn: AsyncConnection,
        record: EntityEmbedding,
        entity_id: str,
        *,
        expected_tenant_id: str,
    ) -> None:
        """Store one embedding using the caller's transaction."""
        await self.store_embeddings_batch_on_connection(
            conn,
            [(record, entity_id)],
            expected_tenant_id=expected_tenant_id,
        )

    async def store_embedding(
        self, record: EntityEmbedding, entity_id: str
    ) -> None:
        tenant_id = normalize_tenant_id(record.tenant_id)
        async with self._client.connection() as conn:
            async with conn.transaction():
                await self.store_embedding_on_connection(
                    conn,
                    record,
                    entity_id,
                    expected_tenant_id=tenant_id,
                )

    async def store_embeddings_batch_on_connection(
        self,
        conn: AsyncConnection,
        records: list[tuple[EntityEmbedding, str]],
        *,
        expected_tenant_id: str,
    ) -> None:
        """Store multiple embeddings using the caller's transaction."""
        if not records:
            return

        tenant_id = normalize_tenant_id(expected_tenant_id)
        params = []
        now = datetime.now(timezone.utc)
        for record, entity_id in records:
            params.append(
                self._embedding_params(
                    record,
                    entity_id,
                    expected_tenant_id=tenant_id,
                    created_at=now,
                )
            )

        await register_vector_async(conn)
        query = sql.SQL("""
            INSERT INTO entity_embeddings (
                id, entity_id, modality, embedding, tenant_id, metadata,
                created_at
            )
            VALUES (%s, %s, %s, %s::vector, %s, %s::jsonb, %s)
        """)
        async with conn.cursor() as cur:
            await cur.executemany(query, params)

    async def store_embeddings_batch(
        self, records: list[tuple[EntityEmbedding, str]]
    ) -> None:
        """Store multiple embeddings in a single batch insert."""
        if not records:
            return

        tenant_id = normalize_tenant_id(records[0][0].tenant_id)
        for record, _ in records:
            require_same_tenant(tenant_id, record.tenant_id)

        async with self._client.connection() as conn:
            async with conn.transaction():
                await self.store_embeddings_batch_on_connection(
                    conn, records, expected_tenant_id=tenant_id
                )

        logger.debug(
            "embeddings_batch_inserted", tenant_id=tenant_id, count=len(records)
        )
