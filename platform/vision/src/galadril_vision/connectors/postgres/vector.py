"""Unified embedding storage and similarity search using pgvectorscale."""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

import orjson
import structlog
from galadril_ontology import (
    GolDenied,
    GolEffect,
    make_gol_state_effect,
    protected_gol_effect,
    protected_gol_effects,
    protected_gol_state_effect,
)
from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from galadril_vision.common.eskg import eskg_write_effect
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
    from galadril_vision.identity.contracts import (
        ResolutionDecision,
        SpatialEvidence,
    )

logger = structlog.get_logger(__name__)


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Rescaling avoids overflow and underflow for finite signed vectors."""
    left_scale = max(abs(value) for value in left)
    right_scale = max(abs(value) for value in right)
    if left_scale == 0 or right_scale == 0:
        return 0.0
    left_norm = math.sqrt(
        math.fsum((value / left_scale) ** 2 for value in left)
    )
    right_norm = math.sqrt(
        math.fsum((value / right_scale) ** 2 for value in right)
    )
    similarity = math.fsum(
        (a / left_scale / left_norm) * (b / right_scale / right_norm)
        for a, b in zip(left, right, strict=True)
    )
    return max(-1.0, min(similarity, 1.0))


class _ConfiguredConnection(Protocol):
    """Connection extension tracking vector codec registration."""

    _vector_registered: bool


@dataclass(frozen=True, slots=True)
class IdentityCandidate:
    """Tenant-scoped vector candidate enriched for LI-ESKG resolution."""

    entity_id: str
    similarity: float
    modality: str
    licorne_identity_id: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy_meters: float = 0.0


class _PrototypeValue(BaseModel):
    """Parses the signed storage envelope after the host authorizes its fields."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )
    entity_id: str
    licorne_identity_id: str = Field(pattern=r"^[0-9]{1,19}$")
    modality: str
    embedding: list[float]
    observation_key: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy_meters: float = Field(default=0.0, ge=0)


@dataclass(frozen=True, slots=True)
class _EmbeddingWrite:
    metadata: dict[str, JsonValue]
    entity_id: str
    row_id: str
    modality: str
    vector: list[float]
    created_at: datetime
    value: dict[str, JsonValue]


class VectorStore:
    """Manages embedding storage and vector similarity search in PostgreSQL."""

    def __init__(
        self, client: PostgresClient, config: PostgresConnectorConfig
    ) -> None:
        """Initializes the vector store.

        Args:
            client: The Postgres client instance.
            config: Configuration settings for the Postgres connector.
        """
        self._client = client
        self._config = config

    async def initialize(self) -> None:
        """Initializes resources or verifies vector store requirements."""
        pass

    def _statement_timeout_ms(self) -> int:
        """Returns the configured statement timeout in milliseconds.

        Returns:
            The timeout value, defaulting to 5000 if invalid or unconfigured.
        """
        raw_value = getattr(self._config, "vector_search_timeout_ms", 5000)
        try:
            timeout_ms = int(raw_value)
        except (TypeError, ValueError):
            timeout_ms = 5000
        return max(timeout_ms, 1)

    def _validate_embedding(
        self, embedding: Sequence[float]
    ) -> Sequence[float]:
        """Validates the dimensions of the provided embedding.

        Args:
            embedding: The embedding vector to validate.

        Returns:
            The validated embedding sequence.

        Raises:
            VectorSearchError: If the vector is empty or dimensions do not match.
        """
        if not embedding:
            raise VectorSearchError("embedding vector is empty")
        if len(embedding) != int(self._config.vector_dimensions):
            raise VectorSearchError(
                "embedding dimension mismatch: "
                f"expected {self._config.vector_dimensions}, got {len(embedding)}"
            )
        return embedding

    async def _ensure_vector_registration(
        self, conn: AsyncConnection[TupleRow]
    ) -> None:
        """Registers the pgvector extension handlers on the connection if missing.

        Args:
            conn: The active database connection.
        """
        if not getattr(conn, "_vector_registered", False):
            await register_vector_async(conn)
            cast(_ConfiguredConnection, conn)._vector_registered = True

    async def find_similar(
        self,
        embedding: Sequence[float],
        modality: str | EmbeddingModality | None,
        tenant_id: str,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """Executes a similarity search and returns entity IDs and scores.

        Args:
            embedding: Query embedding vector.
            modality: Optional modality filter.
            tenant_id: Target tenant identifier.
            top_k: Maximum number of results to return.

        Returns:
            A list of tuples containing the entity ID and similarity score.
        """
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
        """Executes a similarity search and returns entity IDs, scores, and modalities.

        Args:
            embedding: Query embedding vector.
            modality: Optional modality filter.
            tenant_id: Target tenant identifier.
            top_k: Maximum number of results to return.

        Returns:
            A list of tuples containing entity ID, similarity score, and modality.
        """
        async with protected_gol_state_effect(
            operation="read",
            tenant_id=tenant_id,
            destination="identity_state",
            purpose="identity_resolution",
        ):
            tenant_id = normalize_tenant_id(tenant_id)
            validated_vector = self._validate_embedding(embedding)
            limit = max(int(top_k), 1)
            params: tuple[object, ...]

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

            async with self._client.tenant_connection(tenant_id) as conn:
                await self._ensure_vector_registration(conn)

                async with conn.pipeline():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (f"{self._statement_timeout_ms()}ms",),
                        )
                        await cur.execute(query, params)
                        rows = await cur.fetchall()
                        return [(row[0], float(row[1]), row[2]) for row in rows]

    @asynccontextmanager
    async def resolution_transaction(
        self, tenant_id: str, modality: str
    ) -> AsyncIterator[AsyncConnection[TupleRow]]:
        """Serializes candidate discovery and new identity publication."""
        tenant_id = normalize_tenant_id(tenant_id)
        modality = normalize_embedding_modality(modality)
        digest = hashlib.blake2b(digest_size=8, person=b"gala-id")
        digest.update(tenant_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(modality.encode("utf-8"))
        lock_key = int.from_bytes(digest.digest(), "big", signed=True)
        async with self._client.tenant_connection(tenant_id) as conn:
            await conn.execute(
                "SELECT set_config('lock_timeout', %s, true)", ("30s",)
            )
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            yield conn

    async def store_identity_prototype_on_connection(
        self,
        connection: AsyncConnection[TupleRow],
        *,
        tenant_id: str,
        modality: str,
        embedding: Sequence[float],
        decision: ResolutionDecision,
        observation_key: str,
        spatial: SpatialEvidence | None = None,
    ) -> None:
        """Publishes signed prototype evidence before downstream sink work."""
        if decision.entity_id is None or decision.licorne_identity_id is None:
            raise VectorSearchError("unseen identity prototype is incomplete")
        tenant_id = normalize_tenant_id(tenant_id)
        modality = normalize_embedding_modality(modality)
        embedding = self._validate_embedding(embedding)
        value: dict[str, JsonValue] = {
            "entity_id": decision.entity_id,
            "licorne_identity_id": str(decision.licorne_identity_id),
            "modality": modality,
            "embedding": list(embedding),
            "observation_key": observation_key,
            "latitude": spatial.latitude if spatial is not None else None,
            "longitude": spatial.longitude if spatial is not None else None,
            "accuracy_meters": spatial.accuracy_meters
            if spatial is not None
            else 0.0,
        }
        async with protected_gol_state_effect(
            operation="write",
            tenant_id=tenant_id,
            destination="identity_state",
            purpose="identity_resolution",
            instance_id=decision.entity_id,
            proposal=value,
        ) as receipt:
            if not receipt:
                raise VectorSearchError(
                    "Identity prototype provenance unavailable"
                )
            await self._ensure_vector_registration(connection)
            cursor = await connection.execute(
                """
                INSERT INTO identity_prototypes (
                    tenant_id, entity_id, licorne_identity_id, modality,
                    embedding, observation_key, gol_value, gol_receipt
                ) VALUES (%s, %s, %s, %s, %s::vector, %s, %s::jsonb, %s)
                ON CONFLICT (tenant_id, entity_id) DO UPDATE
                SET embedding = EXCLUDED.embedding,
                    observation_key = EXCLUDED.observation_key,
                    gol_value = EXCLUDED.gol_value,
                    gol_receipt = EXCLUDED.gol_receipt
                WHERE identity_prototypes.licorne_identity_id =
                      EXCLUDED.licorne_identity_id
                  AND identity_prototypes.modality = EXCLUDED.modality
                RETURNING entity_id
                """,
                (
                    tenant_id,
                    decision.entity_id,
                    decision.licorne_identity_id,
                    modality,
                    self._validate_embedding(embedding),
                    observation_key,
                    orjson.dumps(value).decode(),
                    receipt,
                ),
            )
            if await cursor.fetchone() is None:
                raise VectorSearchError(
                    "identity prototype conflicts with durable mapping"
                )
            logger.info(
                "identity_prototype_persisted",
                tenant_id=tenant_id,
                modality=modality,
                entity_id=decision.entity_id,
            )

    async def find_resolution_candidates(
        self,
        embedding: Sequence[float],
        modality: str | EmbeddingModality,
        tenant_id: str,
        top_k: int,
        *,
        connection: AsyncConnection[TupleRow] | None = None,
    ) -> list[IdentityCandidate]:
        """Ranks only values whose signed provenance and live read routes pass."""
        tenant_id = normalize_tenant_id(tenant_id)
        modality_key = normalize_embedding_modality(modality)
        target = make_gol_state_effect(
            operation="read",
            tenant_id=tenant_id,
            destination="identity_state",
            purpose="identity_resolution",
        ).target
        vector = self._validate_embedding(embedding)
        if not all(math.isfinite(value) for value in vector):
            raise VectorSearchError("Invalid identity vector")
        if not 1 <= top_k <= 256:
            raise VectorSearchError("Invalid identity candidate limit")
        if connection is None:
            async with self._client.tenant_connection(tenant_id) as conn:
                return await self.find_resolution_candidates(
                    vector, modality_key, tenant_id, top_k, connection=conn
                )
        # Never let an unauthorized row enter a SQL ranking, join, or LIMIT.
        cursor = await connection.execute(
            """
            SELECT entity_id, gol_value, gol_receipt
            FROM identity_prototypes
            WHERE tenant_id = %s AND modality = %s
              AND gol_value IS NOT NULL AND gol_receipt IS NOT NULL
            ORDER BY entity_id
            LIMIT 4097
            """,
            (tenant_id, modality_key),
        )
        rows = await cursor.fetchall()
        if len(rows) > 4096:
            raise VectorSearchError("Identity candidate budget exceeded")
        candidates = []
        for entity_id, value, receipt in rows:
            if not isinstance(value, dict) or not isinstance(receipt, str):
                raise VectorSearchError("Identity provenance unavailable")
            try:
                async with protected_gol_effect(
                    operation="read",
                    target=target,
                    tenant_id=tenant_id,
                    destination="identity_state",
                    purpose="identity_resolution",
                    instance_id=str(entity_id),
                    proposal=value,
                    receipt=receipt,
                ):
                    prototype = _PrototypeValue.model_validate(value)
                    if prototype.entity_id != entity_id:
                        raise VectorSearchError("Identity provenance mismatch")
                    if prototype.modality != modality_key:
                        raise VectorSearchError("Identity modality mismatch")
                    identity = int(prototype.licorne_identity_id)
                    if not 0 < identity < (1 << 63):
                        raise VectorSearchError("Invalid identity prototype")
                    stored = self._validate_embedding(prototype.embedding)
                    candidates.append(
                        IdentityCandidate(
                            entity_id=str(entity_id),
                            similarity=_cosine_similarity(vector, stored),
                            modality=modality_key,
                            licorne_identity_id=identity,
                            latitude=prototype.latitude,
                            longitude=prototype.longitude,
                            accuracy_meters=prototype.accuracy_meters,
                        )
                    )
            except GolDenied:
                logger.info("identity.candidate.denied")
        candidates.sort(key=lambda item: (-item.similarity, item.entity_id))
        return candidates[:top_k]

    async def has_embeddings(
        self,
        *,
        tenant_id: str,
        modality: str | EmbeddingModality | None,
    ) -> bool:
        """Checks whether any embeddings exist matching the criteria.

        Args:
            tenant_id: Target tenant identifier.
            modality: Optional modality filter.

        Returns:
            True if at least one matching record exists, False otherwise.
        """
        async with protected_gol_state_effect(
            operation="read",
            tenant_id=tenant_id,
            destination="identity_state",
            purpose="identity_resolution",
        ):
            tenant_id = normalize_tenant_id(tenant_id)
            params: tuple[object, ...]

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

            async with self._client.tenant_connection(tenant_id) as conn:
                async with conn.pipeline():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (f"{self._statement_timeout_ms()}ms",),
                        )
                        await cur.execute(query, params)
                        result = await cur.fetchone()
                        return result is not None

    async def store_embeddings_batch_on_connection(
        self,
        conn: AsyncConnection[TupleRow],
        records: list[tuple[EntityEmbedding, str]],
        expected_tenant_id: str,
        *,
        gol_target: str | None = None,
    ) -> None:
        """Retains the batch permit through codec setup and each SQL operation."""
        if not records:
            return
        tenant_id = normalize_tenant_id(expected_tenant_id)
        prepared, effects = self._prepare_embeddings(
            records, tenant_id, gol_target
        )
        await self._store_prepared_embeddings(
            conn, prepared, effects, tenant_id
        )

    @staticmethod
    def embedding_write_effect(
        record: EntityEmbedding, entity_id: str, target: str
    ) -> GolEffect:
        """Authorizes the exact vector and its structural provenance together."""
        if {
            "tenant_id",
            "entity_id",
            "modality",
            "embedding",
        } & record.metadata.keys():
            raise VectorSearchError("Conflicting embedding provenance")
        return eskg_write_effect(
            target,
            f"embedding:{record.embedding_id}",
            {
                **record.metadata,
                "tenant_id": normalize_tenant_id(record.tenant_id),
                "entity_id": entity_id,
                "modality": normalize_embedding_modality(record.modality),
                "embedding": cast(list[JsonValue], record.vector),
            },
        )

    def _prepare_embeddings(
        self,
        records: list[tuple[EntityEmbedding, str]],
        tenant_id: str,
        gol_target: str | None = None,
    ) -> tuple[list[_EmbeddingWrite], list[GolEffect]]:
        """Generate IDs once so nested SQL consumes the exact admitted batch."""
        effects: list[GolEffect] = []
        prepared: list[_EmbeddingWrite] = []
        for record, entity_id in records:
            require_same_tenant(tenant_id, record.tenant_id)
            vector = list(self._validate_embedding(record.vector))
            modality = normalize_embedding_modality(record.modality)
            metadata = record.metadata.copy()
            timestamp = metadata.get("timestamp")
            if isinstance(timestamp, datetime):
                metadata["timestamp"] = timestamp.isoformat()
            value: dict[str, JsonValue] = {
                "entity_id": entity_id,
                "modality": modality,
                "embedding": cast(list[JsonValue], vector),
                "metadata": metadata,
            }
            if gol_target:
                effect = self.embedding_write_effect(
                    record, entity_id, gol_target
                )
                if effect.instance_id is None or effect.proposal is None:
                    raise VectorSearchError(
                        "Embedding GOL proposal unavailable"
                    )
                row_id, value = effect.instance_id, effect.proposal
            else:
                row_id = f"emb_{uuid4().hex}"
                effect = make_gol_state_effect(
                    operation="write",
                    tenant_id=tenant_id,
                    destination="eskg",
                    purpose="persistence",
                    derived=True,
                    instance_id=row_id,
                    proposal=value,
                )
            effects.append(effect)
            created_at = record.metadata.get("timestamp") or datetime.now(UTC)
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            if not isinstance(created_at, datetime):
                raise VectorSearchError("Embedding timestamp is invalid")
            prepared.append(
                _EmbeddingWrite(
                    metadata,
                    entity_id,
                    row_id,
                    modality,
                    vector,
                    created_at,
                    value,
                )
            )
        return prepared, effects

    async def _store_prepared_embeddings(
        self,
        conn: AsyncConnection[TupleRow],
        prepared: list[_EmbeddingWrite],
        effects: list[GolEffect],
        tenant_id: str,
    ) -> None:
        async with protected_gol_effects(
            effects, tenant_id=tenant_id
        ) as receipts:
            params = []
            for entry, receipt in zip(prepared, receipts, strict=True):
                if not receipt:
                    raise VectorSearchError("Embedding GOL receipt unavailable")
                params.append(
                    (
                        entry.row_id,
                        entry.entity_id,
                        entry.modality,
                        entry.vector,
                        tenant_id,
                        orjson.dumps(
                            {
                                **entry.metadata,
                                "gol_value": entry.value,
                                "gol_receipt": receipt,
                            }
                        ).decode(),
                        entry.created_at,
                    )
                )
            await self._ensure_vector_registration(conn)
            query = sql.SQL("""
                INSERT INTO entity_embeddings (
                    id, entity_id, modality, embedding, tenant_id, metadata, created_at
                ) VALUES (%s, %s, %s, %s::vector, %s, %s::jsonb, %s)
                ON CONFLICT (tenant_id, id, created_at) DO UPDATE
                SET entity_id = EXCLUDED.entity_id, embedding = EXCLUDED.embedding::vector, metadata = EXCLUDED.metadata::jsonb
            """)
            async with conn.cursor() as cur:
                await cur.executemany(query, params)

    async def store_embeddings_batch(
        self, records: list[tuple[EntityEmbedding, str]]
    ) -> None:
        """Inserts a batch of embeddings inside a new transaction block.

        Args:
            records: A list of tuples containing the EntityEmbedding and entity ID.
        """
        if not records:
            return

        tenant_id = normalize_tenant_id(records[0][0].tenant_id)
        for record, _ in records:
            require_same_tenant(tenant_id, record.tenant_id)

        prepared, effects = self._prepare_embeddings(records, tenant_id)
        async with protected_gol_effects(effects, tenant_id=tenant_id):
            async with self._client.tenant_connection(tenant_id) as conn:
                async with conn.transaction():
                    await self._store_prepared_embeddings(
                        conn, prepared, effects, tenant_id
                    )

        logger.debug(
            "embeddings_batch_inserted", tenant_id=tenant_id, count=len(records)
        )
