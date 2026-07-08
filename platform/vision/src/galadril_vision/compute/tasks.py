"""Asynchronous Postgres batch operations for processing pipeline metadata."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

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

    if isinstance(postgres_config, dict):
        cloned = dict(postgres_config)
        cloned["min_connections"] = min_connections
        cloned["max_connections"] = max_connections
        return cloned

    cloned = copy.copy(postgres_config)
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
            await client.connect(initialize_database_infrastructure=False)
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
) -> list[list[dict[str, Any]]]:
    """Resolves structural target identities using vector similarity search procedures.

    Args:
        state: Runtime database connections state reference tracking active storage layers.
        postgres_config: Target system connection properties configuration metadata blocks.
        inference_results: Unstructured record output arrays returned from active prediction targets.
        tenant_ids: Matching list containing authorization tracking string indices.
        modality: Fallback processing identity used when inference variables lack explicit type definitions.
        threshold: Floating point score constraint determining structural matches.

    Returns:
        Nested list containing localized dictionary elements mapping resolved database parameters.
    """
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
    embedding_presence: dict[tuple[str, str], bool] = {}
    pending_resolutions: list[tuple[dict[str, Any], str, str]] = []

    async def _resolve_item(
        item: dict[str, Any],
        tenant_id_val: str,
        modality_key: str,
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

            presence_key = (tenant_id_val, modality_key)
            has_embeddings = embedding_presence.get(presence_key)
            if has_embeddings is None:
                if hasattr(vector_store, "has_embeddings"):
                    try:
                        has_embeddings = await asyncio.wait_for(
                            vector_store.has_embeddings(
                                tenant_id=tenant_id_val,
                                modality=modality_key,
                            ),
                            timeout=_get_vector_search_timeout_s(
                                postgres_config
                            ),
                        )
                    except TimeoutError:
                        logger.warning(
                            "embedding_presence_check_timed_out",
                            tenant_id=tenant_id_val,
                            modality=modality_key,
                        )
                        has_embeddings = False
                    except Exception as exc:
                        logger.warning(
                            "embedding_presence_check_failed",
                            tenant_id=tenant_id_val,
                            modality=modality_key,
                            error=str(exc),
                        )
                        has_embeddings = False
                else:
                    has_embeddings = True
                embedding_presence[presence_key] = has_embeddings

            if not has_embeddings:
                item["resolved_entity_id"] = (
                    f"unknown_{modality_key}_{uuid4().hex}"
                )
                item["is_unknown"] = True
                logger.debug(
                    "resolve_skipped_empty_embedding_index",
                    tenant_id=tenant_id_val,
                    modality=modality_key,
                )
                return

            try:
                matches = await asyncio.wait_for(
                    vector_store.find_similar(
                        embedding=padded_vector,
                        modality=modality_key,
                        tenant_id=tenant_id_val,
                        top_k=1,
                    ),
                    timeout=_get_vector_search_timeout_s(postgres_config),
                )
            except TimeoutError:
                logger.warning(
                    "vector_similarity_search_timed_out",
                    tenant_id=tenant_id_val,
                    modality=modality_key,
                )
                matches = []
            except Exception as exc:
                logger.warning(
                    "vector_similarity_search_failed",
                    tenant_id=tenant_id_val,
                    modality=modality_key,
                    error=str(exc),
                )
                matches = []

            if matches and matches[0][1] >= threshold:
                item["resolved_entity_id"] = matches[0][0]
                item["is_unknown"] = False
            else:
                item["resolved_entity_id"] = (
                    f"unknown_{modality_key}_{uuid4().hex}"
                )
                item["is_unknown"] = True

    for inference_data, tenant_id in zip(
        inference_results, tenant_ids, strict=False
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

        for item in items:
            if item.get("embedding") is None:
                continue
            item_modality = normalize_embedding_modality(
                item.get("model_name") or item.get("modality") or modality
            )
            item["model_name"] = item_modality
            item.setdefault("modality", item_modality)
            pending_resolutions.append((item, tenant_id_val, item_modality))

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
                _resolve_item(item, tenant_id_val, item_modality, semaphore)
                for item, tenant_id_val, item_modality in pending_resolutions
            )
        )
        logger.info(
            "after_gather",
            step="resolve_entities",
            tasks=len(pending_resolutions),
        )

    return resolved_batch


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
    all_states = []
    all_embeddings = []
    tenant_id_val = "acme"

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
            ) in zip(
                resolved_items,
                record_ids,
                sources,
                tenant_ids,
                event_types,
                raw_payloads,
                strict=False,
            ):
                if not input_data:
                    continue

                tenant_id_val = tenant_id or "acme"
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
                    },
                    timestamp=datetime.now(UTC),
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
                            },
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
                            },
                        ),
                    )

                    modality_key = normalize_embedding_modality(
                        item.get("modality")
                        or item.get("model_name")
                        or modality
                    )
                    all_states.append(
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
                                },
                                tenant_id=tenant_id_val,
                            )
                            all_embeddings.append((emb_record, entity_id))

            batch_tasks = []
            if all_states:
                if hasattr(
                    graph_store, "insert_entity_states_batch_on_connection"
                ):
                    batch_tasks.append(
                        graph_store.insert_entity_states_batch_on_connection(
                            conn,
                            all_states,
                            expected_tenant_id=tenant_id_val,
                        )
                    )
                elif hasattr(graph_store, "insert_entity_states_batch"):
                    batch_tasks.append(
                        graph_store.insert_entity_states_batch(all_states)
                    )

            if all_embeddings:
                if hasattr(
                    vector_store, "store_embeddings_batch_on_connection"
                ):
                    batch_tasks.append(
                        vector_store.store_embeddings_batch_on_connection(
                            conn,
                            all_embeddings,
                            expected_tenant_id=tenant_id_val,
                        )
                    )
                elif hasattr(vector_store, "store_embeddings_batch"):
                    batch_tasks.append(
                        vector_store.store_embeddings_batch(all_embeddings)
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
