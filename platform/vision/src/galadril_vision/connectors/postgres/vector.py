"""Unified embedding storage and similarity search using pgvectorscale."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence
from uuid import uuid4

import orjson
import structlog
from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection, sql

from galadril_vision.common.exceptions import VectorSearchError
from galadril_vision.common.types import (
    EmbeddingModality,
    EntityEmbedding,
    normalize_embedding_modality,
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

    async def initialize(self) -> None:
        """Asynchronously initializes resources or verifies vector store requirements."""
        pass

    def _statement_timeout_ms(self) -> int:
        """Return a bounded statement timeout for vector lookup operations."""
        raw_value = getattr(self._config, "vector_search_timeout_ms", 5000)
        try:
            timeout_ms = int(raw_value)
        except (TypeError, ValueError):
            timeout_ms = 5000
        return max(timeout_ms, 1)

    def _validate_embedding(
        self, embedding: Sequence[float]
    ) -> Sequence[float]:
        """Validates that the provided embedding list matches expected infrastructure dimensions."""
        if not embedding:
            raise VectorSearchError("embedding vector is empty")
        if len(embedding) != int(self._config.vector_dimensions):
            raise VectorSearchError(
                "embedding dimension mismatch: "
                f"expected {self._config.vector_dimensions}, got {len(embedding)}"
            )
        return embedding

    async def find_similar(
        self,
        embedding: Sequence[float],
        modality: str | EmbeddingModality | None,
        tenant_id: str,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """Executes a vector similarity search scoped to tenant and optional modality."""
        rows = await self.find_similar_with_modality(
            embedding=embedding,
            modality=modality,
            tenant_id=tenant_id,
            top_k=top_k,
        )
        return [(entity_id, similarity) for entity_id, similarity, _ in rows]

    async def find_similar_with_modality(
        self,
        embedding: Sequence[float],
        modality: str | EmbeddingModality | None,
        tenant_id: str,
        top_k: int = 5,
    ) -> list[tuple[str, float, str]]:
        """Executes semantic search and returns the source embedding modality."""
        tenant_id = normalize_tenant_id(tenant_id)
        validated_vector = self._validate_embedding(embedding)
        limit = max(int(top_k), 1)

        if modality is None:
            query = sql.SQL("""
                SELECT entity_id, 1.0 - (embedding <=> %s::vector) AS similarity, modality
                FROM entity_embeddings
                WHERE tenant_id = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """)
            params = (validated_vector, tenant_id, validated_vector, limit)
        else:
            modality_key = normalize_embedding_modality(modality)
            query = sql.SQL("""
                SELECT entity_id, 1.0 - (embedding <=> %s::vector) AS similarity, modality
                FROM entity_embeddings
                WHERE tenant_id = %s AND modality = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """)
            params = (
                validated_vector,
                tenant_id,
                modality_key,
                validated_vector,
                limit,
            )

        async with self._client.connection() as conn:
            async with conn.transaction():
                await register_vector_async(conn)
                await conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{self._statement_timeout_ms()}ms",),
                )
                async with conn.cursor() as cur:
                    await cur.execute(query, params)
                    rows = await cur.fetchall()
                    return [(row[0], float(row[1]), row[2]) for row in rows]

    async def has_embeddings(
        self,
        *,
        tenant_id: str,
        modality: str | EmbeddingModality | None,
    ) -> bool:
        """Check whether a scoped embedding set exists before running KNN search."""
        tenant_id = normalize_tenant_id(tenant_id)

        if modality is None:
            query = sql.SQL("""
                SELECT 1
                FROM entity_embeddings
                WHERE tenant_id = %s
                LIMIT 1
            """)
            params = (tenant_id,)
        else:
            modality_key = normalize_embedding_modality(modality)
            query = sql.SQL("""
                SELECT 1
                FROM entity_embeddings
                WHERE tenant_id = %s AND modality = %s
                LIMIT 1
            """)
            params = (tenant_id, modality_key)

        async with self._client.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{self._statement_timeout_ms()}ms",),
                )
                async with conn.cursor() as cur:
                    await cur.execute(query, params)
                    return await cur.fetchone() is not None

    async def store_embeddings_batch_on_connection(
        self,
        conn: AsyncConnection[Any],
        records: list[tuple[EntityEmbedding, str]],
        expected_tenant_id: str,
    ) -> None:
        """Appends multiple embedding mutations to an active pipeline connection batch."""
        if not records:
            return

        tenant_id = normalize_tenant_id(expected_tenant_id)
        params = []

        for record, entity_id in records:
            require_same_tenant(tenant_id, record.tenant_id)

            created_at = record.metadata.get("timestamp") or datetime.now(
                timezone.utc
            )
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)

            record_id = f"emb_{uuid4().hex}"

            params.append(
                (
                    record_id,
                    entity_id,
                    normalize_embedding_modality(record.modality),
                    list(self._validate_embedding(record.vector)),
                    tenant_id,
                    orjson.dumps(record.metadata).decode("utf-8"),
                    created_at,
                )
            )

        await register_vector_async(conn)

        query = sql.SQL("""
            INSERT INTO entity_embeddings (
                id, entity_id, modality, embedding, tenant_id, metadata, created_at
            )
            VALUES (%s, %s, %s, %s::vector, %s, %s::jsonb, %s)
            ON CONFLICT (tenant_id, id, created_at) DO UPDATE 
            SET entity_id = EXCLUDED.entity_id,
                embedding = EXCLUDED.embedding::vector,
                metadata = EXCLUDED.metadata::jsonb
        """)

        async with conn.cursor() as cur:
            await cur.executemany(query, params)

    async def store_embeddings_batch(
        self, records: list[tuple[EntityEmbedding, str]]
    ) -> None:
        """Store multiple embeddings in a single batch insert wrapping its own transaction."""
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
