"""Asynchronous Postgres batch operations for processing pipeline metadata."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import orjson
import structlog

from galadril_vision.common.types import normalize_embedding_modality
from galadril_vision.compute.helpers import (
    _build_state_value,
    _extract_embedding_items,
    _get_vector_dimensions,
    _get_vector_search_timeout_s,
    _pad_embedding_if_needed,
)
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import GraphStore
from galadril_vision.connectors.postgres.vector import VectorStore
from galadril_vision.identity.licorne import (
    CandidateEvidence,
    IdentityResolver,
    ResolutionRequest,
    SpatialEvidence,
)

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class PostgresRuntimeState:
    """Runtime cache containing connection handles and store interfaces.

    Attributes:
        client: Cached Postgres client infrastructure mapping.
        vector_store: Vector operations layer client instance.
        graph_store: Property graph data storage layout interface.
        init_lock: Exclusion lock guarding worker initialization blocks.
    """

    client: PostgresClient | None = None
    vector_store: VectorStore | None = None
    graph_store: GraphStore | None = None
    init_lock: asyncio.Lock | None = None


def _clone_postgres_config(postgres_config: Any) -> Any:
    """Clones the postgres configuration object while sanitizing connection pool ranges."""
    if isinstance(postgres_config, dict):
        min_connections = max(int(postgres_config.get("min_connections", 1)), 1)
        max_connections = max(
            int(postgres_config.get("max_connections", min_connections)),
            min_connections,
        )
        cloned = dict(postgres_config)
        cloned["min_connections"] = min_connections
        cloned["max_connections"] = max_connections
        return cloned

    min_connections = max(
        int(getattr(postgres_config, "min_connections", 1)), 1
    )
    max_connections = max(
        int(getattr(postgres_config, "max_connections", min_connections)),
        min_connections,
    )

    if hasattr(postgres_config, "model_copy"):
        return postgres_config.model_copy(
            update={
                "min_connections": min_connections,
                "max_connections": max_connections,
            }
        )

    cloned = cast(Any, copy.copy(postgres_config))
    cloned.min_connections = min_connections
    cloned.max_connections = max_connections
    return cloned


async def get_pg_stores(
    postgres_config: Any, state: PostgresRuntimeState
) -> tuple[PostgresClient, VectorStore, GraphStore]:
    """Retrieves or initializes cached structural database layer entities.

    Args:
        postgres_config: Source connection parameters layout config context.
        state: Runtime state reference managing cache properties.

    Returns:
        A tuple containing an operational client, vector store, and graph store.
    """
    if (
        state.client is not None
        and state.vector_store is not None
        and state.graph_store is not None
    ):
        return state.client, state.vector_store, state.graph_store

    if state.init_lock is None:
        state.init_lock = asyncio.Lock()

    async with state.init_lock:
        if (
            state.client is not None
            and state.vector_store is not None
            and state.graph_store is not None
        ):
            return state.client, state.vector_store, state.graph_store

        runtime_config = _clone_postgres_config(postgres_config)
        logger.debug(
            "before_get_pg",
            min_connections=getattr(runtime_config, "min_connections", None),
            max_connections=getattr(runtime_config, "max_connections", None),
        )
        client = PostgresClient(runtime_config)

        try:
            await client.connect()
            state.client = client
            state.vector_store = VectorStore(client, runtime_config)
            state.graph_store = GraphStore(client, runtime_config)
            logger.info(
                "postgres_pool_initialized_on_worker",
                min_connections=getattr(
                    runtime_config, "min_connections", None
                ),
                max_connections=getattr(
                    runtime_config, "max_connections", None
                ),
            )
        except Exception:
            state.client = None
            state.vector_store = None
            state.graph_store = None
            raise

    assert state.client is not None
    assert state.vector_store is not None
    assert state.graph_store is not None
    return state.client, state.vector_store, state.graph_store


def _vector_concurrency_limit(postgres_config: Any, item_count: int) -> int:
    """Calculates maximum execution slots allowable given active pool targets."""
    max_connections = max(
        int(getattr(postgres_config, "max_connections", 5)),
        1,
    )
    return max(1, min(max_connections, item_count))


async def resolve_entities_batch(
    *,
    state: PostgresRuntimeState,
    postgres_config: Any,
    inference_results: list[dict[str, Any]],
    tenant_ids: list[str],
    modality: str,
    threshold: float,
    resolver: IdentityResolver | None = None,
    records: list[dict[str, Any]] | None = None,
    candidate_top_k: int = 8,
) -> list[list[dict[str, Any]]]:
    """Resolves extracted entities through pgvector candidates and LI-ESKG.

    Args:
        state: Runtime database connections state reference tracking active storage layers.
        postgres_config: Target system connection properties configuration metadata blocks.
        inference_results: Unstructured record output arrays returned from active prediction targets.
        tenant_ids: Matching list containing authorization tracking string indices.
        modality: Fallback processing identity used when inference variables lack explicit type definitions.
        threshold: Calibrated vector-similarity midpoint.
        resolver: Actor-local LI-ESKG runtime, or None for the kill-switch path.
        records: Canonical input records aligned with inference results.
        candidate_top_k: Bounded pgvector candidate domain size.

    Returns:
        Nested list containing localized dictionary elements mapping resolved database parameters.
    """
    if not -1.0 <= threshold <= 1.0:
        raise ValueError(
            "vector similarity midpoint must be within [-1.0, 1.0]"
        )
    if not 1 <= candidate_top_k <= 256:
        raise ValueError("candidate_top_k must be within [1, 256]")
    logger.debug(
        "before_get_pg",
        step="resolve_entities",
        items=len(inference_results),
    )
    _, vector_store, _ = await get_pg_stores(postgres_config, state)
    logger.debug(
        "after_get_pg",
        step="resolve_entities",
        items=len(inference_results),
    )
    resolved_batch: list[list[dict[str, Any]]] = []
    pending_resolutions: list[
        tuple[dict[str, Any], str, str, dict[str, Any], int]
    ] = []

    async def _resolve_item(
        item: dict[str, Any],
        tenant_id_val: str,
        modality_key: str,
        record: dict[str, Any],
        ordinal: int,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            vector = item.get("embedding")
            if vector is None:
                return

            padded_vector = _pad_embedding_if_needed(
                vector,
                expected_dim=_get_vector_dimensions(postgres_config),
            )
            if padded_vector is None:
                return

            item["embedding"] = padded_vector
            item["model_name"] = modality_key
            item.setdefault("modality", modality_key)
            candidates: list[Any]
            try:
                if hasattr(vector_store, "find_resolution_candidates"):
                    candidates = await asyncio.wait_for(
                        vector_store.find_resolution_candidates(
                            embedding=padded_vector,
                            modality=modality_key,
                            tenant_id=tenant_id_val,
                            top_k=candidate_top_k,
                        ),
                        timeout=_get_vector_search_timeout_s(postgres_config),
                    )
                else:
                    matches = await asyncio.wait_for(
                        vector_store.find_similar(
                            embedding=padded_vector,
                            modality=modality_key,
                            tenant_id=tenant_id_val,
                            top_k=candidate_top_k,
                        ),
                        timeout=_get_vector_search_timeout_s(postgres_config),
                    )
                    candidates = [
                        _LegacyCandidate(entity_id, similarity)
                        for entity_id, similarity in matches
                    ]
            except TimeoutError as error:
                raise RuntimeError(
                    "candidate search timed out; refusing to create an identity "
                    "from an unverified empty domain"
                ) from error
            except Exception as error:
                raise RuntimeError(
                    "candidate search failed; refusing to create an identity "
                    "from an unverified empty domain"
                ) from error

            if resolver is None:
                if candidates and candidates[0].similarity >= threshold:
                    item["resolved_entity_id"] = candidates[0].entity_id
                    item["is_unknown"] = False
                    item["resolution_action"] = "assign_legacy"
                else:
                    item["resolved_entity_id"] = _fallback_entity_id(
                        tenant_id_val,
                        str(record.get("record_id") or "record"),
                        modality_key,
                        ordinal,
                    )
                    item["is_unknown"] = True
                    item["resolution_action"] = "create_legacy"
                return

            spatial = _spatial_evidence(record.get("spatial"))
            candidate_evidence = tuple(
                CandidateEvidence(
                    entity_id=candidate.entity_id,
                    similarity=candidate.similarity,
                    licorne_identity_id=getattr(
                        candidate, "licorne_identity_id", None
                    ),
                    spatial=_candidate_spatial(candidate),
                )
                for candidate in candidates
            )
            probability = _pipeline_probability(item)
            record_id = str(record.get("record_id") or "record")
            decision = await resolver.resolve(
                ResolutionRequest(
                    tenant_id=tenant_id_val,
                    observation_key=f"{record_id}:{modality_key}:{ordinal}",
                    source=str(record.get("source") or "unknown"),
                    modality=modality_key,
                    event_time_micros=_event_time_micros(
                        record.get("timestamp")
                    ),
                    pipeline_probability=probability,
                    candidates=candidate_evidence,
                    spatial=spatial,
                    vector_similarity_midpoint=threshold,
                )
            )
            item.update(
                {
                    "resolved_entity_id": decision.entity_id,
                    "is_unknown": decision.entity_id is None,
                    "resolution_action": decision.action,
                    "resolution_probability": decision.selected_probability,
                    "resolution_probabilities": decision.probabilities,
                    "licorne_identity_id": decision.licorne_identity_id,
                    "licorne_observation_id": decision.observation_id,
                    "licorne_decision_id": decision.decision_id,
                    "licorne_inference_id": decision.inference_id,
                    "licorne_version": decision.final_version,
                    "licorne_created_identity": decision.created_identity,
                    "licorne_iterations": decision.iterations,
                    "licorne_residual": decision.residual,
                    "licorne_exact": decision.exact,
                    "h3_cell": decision.h3_cell,
                }
            )
            if spatial is not None:
                item["spatial"] = {
                    "latitude": spatial.latitude,
                    "longitude": spatial.longitude,
                    "accuracy_meters": spatial.accuracy_meters,
                }

    aligned_records = records or [{} for _ in inference_results]
    for inference_data, tenant_id, record in zip(
        inference_results, tenant_ids, aligned_records, strict=False
    ):
        if not inference_data or inference_data.get("error"):
            resolved_batch.append([])
            continue

        tenant_id_val = tenant_id or "acme"
        prediction = inference_data.get("prediction") or {}
        model_name = inference_data.get("model_name") or modality
        items = _extract_embedding_items(prediction, model_name)
        raw_modality = inference_data.get("raw_modality")
        model_version = inference_data.get("model_version")
        confidence = inference_data.get("confidence")

        for item in items:
            if raw_modality is not None:
                item.setdefault("raw_modality", raw_modality)
            if model_version is not None:
                item.setdefault("model_version", model_version)
            if confidence is not None:
                item.setdefault("confidence", confidence)

        for ordinal, item in enumerate(items):
            if item.get("embedding") is None:
                continue
            item_modality = normalize_embedding_modality(
                item.get("model_name") or item.get("modality") or modality
            )
            item["model_name"] = item_modality
            item.setdefault("modality", item_modality)
            pending_resolutions.append(
                (item, tenant_id_val, item_modality, record, ordinal)
            )

        resolved_batch.append(items)

    if pending_resolutions:
        logger.info(
            "before_gather",
            step="resolve_entities",
            tasks=len(pending_resolutions),
        )
        semaphore = asyncio.Semaphore(
            _vector_concurrency_limit(postgres_config, len(pending_resolutions))
        )
        await asyncio.gather(
            *(
                _resolve_item(
                    item,
                    tenant_id_val,
                    item_modality,
                    record,
                    ordinal,
                    semaphore,
                )
                for (
                    item,
                    tenant_id_val,
                    item_modality,
                    record,
                    ordinal,
                ) in pending_resolutions
            )
        )
        logger.info(
            "after_gather",
            step="resolve_entities",
            tasks=len(pending_resolutions),
        )

    return resolved_batch


@dataclass(frozen=True, slots=True)
class _LegacyCandidate:
    """Compatibility shape for vector-store test doubles and kill-switch mode."""

    entity_id: str
    similarity: float


def _pipeline_probability(item: dict[str, Any]) -> float:
    """Extracts a finite calibrated probability from model output."""
    for key in ("match_probability", "probability", "confidence"):
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        probability = float(value)
        if not 0.0 <= probability <= 1.0:
            raise ValueError(f"{key} must be a probability within [0.0, 1.0]")
        return probability
    return 0.5


def _spatial_evidence(value: object) -> SpatialEvidence | None:
    """Parses canonical spatial JSON into fixed-size resolver evidence."""
    if not isinstance(value, dict):
        return None
    latitude = value.get("latitude")
    longitude = value.get("longitude")
    accuracy = value.get("accuracy_meters", 0.0)
    if not isinstance(latitude, (int, float)) or not isinstance(
        longitude, (int, float)
    ):
        return None
    if not isinstance(accuracy, (int, float)):
        accuracy = 0.0
    return SpatialEvidence(float(latitude), float(longitude), float(accuracy))


def _candidate_spatial(candidate: object) -> SpatialEvidence | None:
    """Returns candidate location only when both PostGIS coordinates exist."""
    latitude = getattr(candidate, "latitude", None)
    longitude = getattr(candidate, "longitude", None)
    if latitude is None or longitude is None:
        return None
    return SpatialEvidence(
        float(latitude),
        float(longitude),
        float(getattr(candidate, "accuracy_meters", 0.0)),
    )


def _event_time_micros(value: object) -> int:
    """Converts canonical event time to LI-ESKG microseconds."""
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, (int, float)):
        return int(float(value) * 1_000.0)
    else:
        timestamp = datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return int(timestamp.timestamp() * 1_000_000.0)


def _event_datetime(value: object) -> datetime:
    """Returns a timezone-aware datetime for PostgreSQL event persistence."""
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, (int, float)):
        timestamp = datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    else:
        timestamp = datetime.now(UTC)
    return (
        timestamp
        if timestamp.tzinfo is not None
        else timestamp.replace(tzinfo=UTC)
    )


def _fallback_entity_id(
    tenant_id: str, record_id: str, modality: str, ordinal: int
) -> str:
    """Creates a replay-stable ID only when LI-ESKG is explicitly disabled."""
    digest = hashlib.blake2b(digest_size=16, person=b"gala-legacy-id")
    digest.update(tenant_id.encode())
    digest.update(record_id.encode())
    digest.update(modality.encode())
    digest.update(ordinal.to_bytes(4, "big"))
    return f"legacy_{digest.hexdigest()}"


async def _upsert_identity_link(
    conn: Any,
    *,
    tenant_id: str,
    entity_id: str,
    licorne_identity_id: int,
    licorne_version: int,
) -> None:
    """Persists a bijective identity link and rejects remapping attempts."""
    cursor = await conn.execute(
        """
        INSERT INTO identity_links (
            tenant_id, entity_id, licorne_identity_id,
            licorne_version, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (tenant_id, entity_id) DO UPDATE
        SET licorne_version = GREATEST(
                identity_links.licorne_version,
                EXCLUDED.licorne_version
            ),
            updated_at = NOW()
        WHERE identity_links.licorne_identity_id = EXCLUDED.licorne_identity_id
        RETURNING licorne_identity_id
        """,
        (tenant_id, entity_id, licorne_identity_id, licorne_version),
    )
    row = await cursor.fetchone()
    if row is None or int(row[0]) != licorne_identity_id:
        raise RuntimeError(
            "PostgreSQL entity is already linked to a different LI-ESKG identity"
        )


async def sink_to_db_batch(
    *,
    state: PostgresRuntimeState,
    postgres_config: Any,
    resolved_items: list[list[dict[str, Any]]],
    record_ids: list[str],
    sources: list[str],
    tenant_ids: list[str],
    event_types: list[str],
    raw_payloads: list[dict[str, Any] | None],
    event_times: list[datetime | str | int | float | None] | None = None,
    spatials: list[dict[str, Any] | None] | None = None,
    entity_type: str,
    modality: str,
    edge_type: str,
    state_type: str,
) -> list[bool]:
    """Persists resolved model entity graph mappings, state properties, and structural embeddings.

    Args:
        state: Connection routing handles state container context.
        postgres_config: Storage environment properties configuration mappings.
        resolved_items: Collection arrays parsing identified entity models and vectors.
        record_ids: Unique primary file mapping sequence keys.
        sources: Origin tracing label descriptors mapping record paths.
        tenant_ids: Unique target tracking authorization namespace identifiers.
        event_types: Type tags defining runtime execution states.
        raw_payloads: Source parameter dict contexts containing additional authorization configurations.
        event_times: Canonical event timestamps aligned with input records.
        spatials: Canonical WGS84 evidence aligned with input records.
        entity_type: Fallback graph vertex label applied when target components evaluate missing fields.
        modality: Global structural modality indicator.
        edge_type: Relationship edge indicator designation.
        state_type: Entity metadata classification string category identifier.

    Returns:
        A list of booleans indicating success flags mapping to matching processed item entries.
    """
    logger.debug("before_get_pg", step="sink_to_db", items=len(resolved_items))
    pg_client, vector_store, graph_store = await get_pg_stores(
        postgres_config, state
    )
    logger.debug("after_get_pg", step="sink_to_db", items=len(resolved_items))
    states_by_tenant: dict[str, list[Any]] = {}
    embeddings_by_tenant: dict[str, list[Any]] = {}
    aligned_event_times = event_times or [None] * len(resolved_items)
    aligned_spatials = spatials or [None] * len(resolved_items)

    from galadril_vision.common.types import (
        EntityEmbedding,
        EntityStateRecord,
        EventRecord,
        EventType,
        GraphEdge,
        GraphVertex,
        normalize_embedding_modality,
    )

    def _normalize_authz_tuple(
        tuple_data: Any, tenant_id_val: str
    ) -> dict[str, Any] | None:
        """Converts arbitrary authorization input variables into standard relational structures."""
        if not isinstance(tuple_data, dict):
            return None

        resource = tuple_data.get("resource") or tuple_data.get("object")
        relation = tuple_data.get("relation") or tuple_data.get("permission")
        subject = (
            tuple_data.get("subject")
            or tuple_data.get("principal")
            or tuple_data.get("user")
        )

        if not isinstance(resource, str) or not resource:
            return None
        if not isinstance(relation, str) or not relation:
            return None
        if not isinstance(subject, str) or not subject:
            return None

        normalized: dict[str, Any] = {
            "tenant_id": tenant_id_val,
            "resource": resource,
            "relation": relation,
            "subject": subject,
        }

        source_principal = tuple_data.get("source_principal")
        if isinstance(source_principal, str) and source_principal:
            normalized["source_principal"] = source_principal

        return normalized

    async with pg_client.connection() as conn:
        async with conn.transaction():
            if hasattr(graph_store, "prepare_connection"):
                await graph_store.prepare_connection(conn)

            for (
                input_data,
                record_id,
                source,
                tenant_id,
                event_type,
                raw_payload,
                event_time,
                spatial,
            ) in zip(
                resolved_items,
                record_ids,
                sources,
                tenant_ids,
                event_types,
                raw_payloads,
                aligned_event_times,
                aligned_spatials,
                strict=False,
            ):
                if not input_data:
                    continue

                tenant_id_val = tenant_id or "acme"
                parsed_spatial = _spatial_evidence(spatial)
                event = EventRecord(
                    event_id=f"evt_{record_id}",
                    tenant_id=tenant_id_val,
                    event_type=EventType.from_str(event_type)
                    if event_type
                    else EventType.OBSERVATION,
                    properties={
                        "source": source or "unknown",
                        "record_id": record_id,
                        "modality": modality,
                        "h3_cell": next(
                            (
                                item.get("h3_cell")
                                for item in input_data
                                if item.get("h3_cell") is not None
                            ),
                            None,
                        ),
                    },
                    timestamp=_event_datetime(event_time),
                    location_coords=(
                        [parsed_spatial.latitude, parsed_spatial.longitude]
                        if parsed_spatial is not None
                        else None
                    ),
                )
                await graph_store.insert_event_on_connection(conn, event)

                if isinstance(raw_payload, dict):
                    authz_block = raw_payload.get("authz")
                    if isinstance(authz_block, dict):
                        authz_tuples = authz_block.get("tuples")
                        if isinstance(authz_tuples, list) and authz_tuples:
                            canonical_tuples = [
                                normalized
                                for item in authz_tuples
                                if (
                                    normalized := _normalize_authz_tuple(
                                        item, tenant_id_val
                                    )
                                )
                            ]
                            if not canonical_tuples:
                                logger.warning(
                                    "authz_outbox_payload_skipped",
                                    record_id=record_id,
                                    tenant_id=tenant_id_val,
                                )
                                continue
                            await conn.execute(
                                """
                                INSERT INTO authz_outbox (
                                    tenant_id, object_id, tuples_json, attempts,
                                    next_retry_at, created_at, updated_at
                                ) VALUES (%s, %s, %s, 0, NOW(), NOW(), NOW())
                                ON CONFLICT (tenant_id, object_id) DO UPDATE
                                SET tuples_json = EXCLUDED.tuples_json, updated_at = NOW()
                                """,
                                (
                                    tenant_id_val,
                                    str(record_id),
                                    orjson.dumps(canonical_tuples).decode(
                                        "utf-8"
                                    ),
                                ),
                            )

                for item in input_data:
                    entity_id = item.get("resolved_entity_id")
                    if not entity_id:
                        continue

                    await graph_store.ensure_vertex_on_connection(
                        conn,
                        GraphVertex(
                            vertex_id=entity_id,
                            label=item.get("entity_type")
                            or item.get("label_type")
                            or entity_type,
                            tenant_id=tenant_id_val,
                            properties={
                                "is_unknown": item.get("is_unknown", True),
                                "modality": item.get("modality") or modality,
                                "raw_modality": item.get("raw_modality"),
                                "label": item.get("label"),
                                "licorne_identity_id": item.get(
                                    "licorne_identity_id"
                                ),
                                "resolution_probability": item.get(
                                    "resolution_probability"
                                ),
                            },
                        ),
                    )

                    licorne_identity_id = item.get("licorne_identity_id")
                    if isinstance(licorne_identity_id, int):
                        await _upsert_identity_link(
                            conn,
                            tenant_id=tenant_id_val,
                            entity_id=str(entity_id),
                            licorne_identity_id=licorne_identity_id,
                            licorne_version=int(
                                item.get("licorne_version") or 0
                            ),
                        )

                    await graph_store.create_edge_on_connection(
                        conn,
                        GraphEdge(
                            source_vertex_id=entity_id,
                            target_vertex_id=event.event_id,
                            edge_type=item.get("edge_type") or edge_type,
                            tenant_id=tenant_id_val,
                            properties={
                                "modality": item.get("modality") or modality,
                                "raw_modality": item.get("raw_modality"),
                                "confidence": item.get("confidence"),
                                "resolution_probability": item.get(
                                    "resolution_probability"
                                ),
                                "licorne_decision_id": item.get(
                                    "licorne_decision_id"
                                ),
                            },
                        ),
                    )

                    modality_key = normalize_embedding_modality(
                        item.get("modality")
                        or item.get("model_name")
                        or modality
                    )
                    states_by_tenant.setdefault(tenant_id_val, []).append(
                        EntityStateRecord(
                            entity_id=entity_id,
                            event_id=event.event_id,
                            state_type=item.get("state_type") or state_type,
                            state_value=_build_state_value(
                                item,
                                modality=modality_key,
                                model_name=modality_key,
                                event_id=event.event_id,
                            ),
                            event_time=event.timestamp,
                            tenant_id=tenant_id_val,
                        )
                    )

                    if item.get("embedding") is not None:
                        vector_val = _pad_embedding_if_needed(
                            item.get("embedding"),
                            expected_dim=_get_vector_dimensions(
                                postgres_config
                            ),
                        )
                        if vector_val is not None:
                            emb_record = EntityEmbedding(
                                modality=modality_key,
                                vector=vector_val,
                                metadata={
                                    "event_id": event.event_id,
                                    "state_type": item.get("state_type")
                                    or state_type,
                                    "entity_type": item.get("entity_type")
                                    or entity_type,
                                    "raw_modality": item.get("raw_modality"),
                                    "model_name": modality_key,
                                    "model_version": item.get("model_version"),
                                    "licorne_identity_id": item.get(
                                        "licorne_identity_id"
                                    ),
                                    "licorne_version": item.get(
                                        "licorne_version"
                                    ),
                                },
                                tenant_id=tenant_id_val,
                            )
                            embeddings_by_tenant.setdefault(
                                tenant_id_val, []
                            ).append((emb_record, entity_id))

            batch_tasks = []
            for batch_tenant_id, states in states_by_tenant.items():
                if states:
                    if hasattr(
                        graph_store,
                        "insert_entity_states_batch_on_connection",
                    ):
                        batch_tasks.append(
                            graph_store.insert_entity_states_batch_on_connection(
                                conn,
                                states,
                                expected_tenant_id=batch_tenant_id,
                            )
                        )
                    elif hasattr(graph_store, "insert_entity_states_batch"):
                        batch_tasks.append(
                            graph_store.insert_entity_states_batch(states)
                        )

            for batch_tenant_id, embeddings in embeddings_by_tenant.items():
                if embeddings:
                    if hasattr(
                        vector_store,
                        "store_embeddings_batch_on_connection",
                    ):
                        batch_tasks.append(
                            vector_store.store_embeddings_batch_on_connection(
                                conn,
                                embeddings,
                                expected_tenant_id=batch_tenant_id,
                            )
                        )
                    elif hasattr(vector_store, "store_embeddings_batch"):
                        batch_tasks.append(
                            vector_store.store_embeddings_batch(embeddings)
                        )

            if batch_tasks:
                logger.info(
                    "before_gather",
                    step="sink_to_db",
                    tasks=len(batch_tasks),
                )
                await asyncio.gather(*batch_tasks)
                logger.info(
                    "after_gather",
                    step="sink_to_db",
                    tasks=len(batch_tasks),
                )

    return [True] * len(resolved_items)
