"""Daft UDFs for the vision pipeline."""

from __future__ import annotations

import asyncio
import os
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

import boto3
import cv2
import daft
import numpy as np
import orjson
import structlog
from daft import DataType, Series
from numpy.typing import NDArray

from galadril_inference.storage.s3 import S3Loader

logger = structlog.get_logger(__name__)

_S3_CLIENT = None
_INFERENCE_ENGINES: dict[str, Any] = {}
_PG_CLIENT = None
_VECTOR_STORE = None
_GRAPH_STORE = None
_LOOP = None

_S3_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_PG_THREAD_LOCK = threading.Lock()
_LOOP_LOCK = threading.Lock()


class CustomS3Loader(S3Loader):
    """Backward-compatible S3 loader that now inherits real upload support."""
    pass


def _start_background_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Entrypoint running inside the daemon thread context."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Returns a fully operational running background event loop wrapped in a daemon thread."""
    global _LOOP
    if _LOOP is None:
        with _LOOP_LOCK:
            if _LOOP is None:
                new_loop = asyncio.new_event_loop()
                t = threading.Thread(
                    target=_start_background_loop,
                    args=(new_loop,),
                    name="GaladrilAsyncWorkerLoop",
                    daemon=True,
                )
                t.start()
                _LOOP = new_loop
                logger.info("background_asyncio_loop_started_via_daemon_thread")
    return _LOOP


def _run_async_blocking(coro) -> Any:
    """Safely executes an async coroutine inside the background loop and blocks for the result."""
    loop = _get_or_create_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def _get_s3_client(
    endpoint_url: str | None,
    region_name: str | None,
    access_key: str | None,
    secret_key: str | None,
) -> Any:
    """Initializes and caches a thread-safe boto3 S3 client singleton on the worker."""
    global _S3_CLIENT
    if _S3_CLIENT is None:
        with _S3_LOCK:
            if _S3_CLIENT is None:
                _S3_CLIENT = boto3.client(
                    "s3",
                    region_name=region_name or "us-east-1",
                    endpoint_url=endpoint_url,
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                )
                logger.info("s3_client_initialized_on_worker")
    return _S3_CLIENT


def _get_inference_engine(
    model_name: str,
    bucket: str,
    prefix: str,
    endpoint_url: str | None,
    region_name: str | None,
    access_key: str | None,
    secret_key: str | None,
) -> Any:
    """Initializes and caches the specific model inference engine instance."""
    global _INFERENCE_ENGINES
    if model_name not in _INFERENCE_ENGINES:
        with _INFERENCE_LOCK:
            if model_name not in _INFERENCE_ENGINES:
                if access_key:
                    os.environ["AWS_ACCESS_KEY_ID"] = access_key
                if secret_key:
                    os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
                if region_name:
                    os.environ["AWS_DEFAULT_REGION"] = region_name
                    os.environ["AWS_REGION"] = region_name

                from galadril_inference import InferenceEngine

                loader = CustomS3Loader(
                    bucket=bucket, prefix=prefix, endpoint_url=endpoint_url
                )
                engine = InferenceEngine(loader=loader)
                engine.load_model(model_name)
                _INFERENCE_ENGINES[model_name] = engine
                logger.info("model_loaded_on_worker", model=model_name)
    return _INFERENCE_ENGINES[model_name]


async def _get_pg_stores(postgres_config: Any) -> tuple[Any, Any, Any]:
    """Initializes or retrieves active database clients and store singletons asynchronously."""
    global _PG_CLIENT, _VECTOR_STORE, _GRAPH_STORE
    if _PG_CLIENT is None:
        with _PG_THREAD_LOCK:
            if _PG_CLIENT is None:
                from galadril_vision.connectors.postgres.client import PostgresClient
                from galadril_vision.connectors.postgres.graph import GraphStore
                from galadril_vision.connectors.postgres.vector import VectorStore

                postgres_config.min_connections = 1
                postgres_config.max_connections = 5

                client = PostgresClient(postgres_config)
                await client.connect()

                _VECTOR_STORE = VectorStore(client, postgres_config)
                _GRAPH_STORE = GraphStore(client, postgres_config)
                _PG_CLIENT = client
                logger.info("postgres_pool_initialized_on_worker")

    return _PG_CLIENT, _VECTOR_STORE, _GRAPH_STORE


def _pad_embedding_if_needed(vector: Any, expected_dim: int = 1024) -> list[float] | None:
    """Pads 1D vector layouts up to the expected dimension or throws an error if exceeded."""
    if vector is None:
        return None
    
    v_arr = np.asarray(vector, dtype=np.float32)
    
    if v_arr.ndim != 1:
        v_arr = v_arr.ravel()
        
    current_dim = v_arr.shape[0]
    
    if current_dim == expected_dim:
        return v_arr.tolist()
        
    if current_dim < expected_dim:
        pad_size = expected_dim - current_dim
        return np.pad(v_arr, (0, pad_size), mode='constant').tolist()
        
    raise ValueError(
        f"Embedding dimension {current_dim} exceeds maximum allowed limit of {expected_dim}."
    )


@daft.udf(return_dtype=DataType.python())
def download_images_udf(
    storage_paths: Series,
    record_ids: Series,
    *,
    bucket: str,
    prefix: str,
    endpoint_url: str | None,
    region_name: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
) -> list[NDArray[np.uint8] | None]:
    """Downloads raw images sequentially from S3 bucket configurations across Ray workers."""
    client = _get_s3_client(endpoint_url, region_name, access_key, secret_key)
    results: list[NDArray[np.uint8] | None] = []

    for storage_path, record_id in zip(storage_paths, record_ids):
        if not storage_path:
            results.append(None)
            continue

        try:
            if storage_path.startswith("s3://"):
                parts = storage_path[5:].split("/", 1)
                s3_bucket, key = parts[0], parts[1] if len(parts) > 1 else ""
            else:
                s3_bucket = bucket
                key = f"{prefix}/{storage_path}".strip("/")

            response = client.get_object(Bucket=s3_bucket, Key=key)
            nparr = np.frombuffer(response["Body"].read(), np.uint8)
            image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if image is None:
                logger.warning("image_decode_failed", record_id=record_id)

            results.append(cast(NDArray[np.uint8], image))
        except Exception as exc:
            logger.warning("image_download_failed", record_id=record_id, error=str(exc))
            results.append(None)

    return results


@daft.udf(return_dtype=DataType.python())
def run_inference_udf(
    images: Series,
    record_ids: Series,
    *,
    artifact_bucket: str,
    artifact_prefix: str,
    artifact_endpoint_url: str | None,
    model_name: str,
    action: str = "embed",
    artifact_region_name: str | None = None,
    artifact_access_key: str | None = None,
    artifact_secret_key: str | None = None,
) -> list[dict[str, Any]]:
    """Runs synchronous inference computations on local cached models per worker node."""
    from galadril_inference import PredictionRequest

    init_error = None
    engine = None

    try:
        engine = _get_inference_engine(
            model_name,
            artifact_bucket,
            artifact_prefix,
            artifact_endpoint_url,
            artifact_region_name,
            artifact_access_key,
            artifact_secret_key,
        )
    except Exception:
        init_error = traceback.format_exc()
        logger.error("inference_engine_initialization_failed", model_name=model_name, error=init_error)

    results: list[dict[str, Any]] = []

    for image, record_id in zip(images, record_ids):
        if init_error:
            results.append({"record_id": record_id, "error": init_error})
            continue

        if image is None:
            results.append({"record_id": record_id, "error": "No image data"})
            continue

        try:
            req = PredictionRequest(
                model_name=model_name,
                features={"action": action, "image": image},
            )
            result = engine.predict(req)
            results.append(
                {
                    "record_id": record_id,
                    "prediction": result.prediction,
                    "confidence": result.confidence,
                    "model_version": result.model_version,
                    "error": None,
                }
            )
        except Exception:
            err_msg = traceback.format_exc()
            logger.error("inference_fatal_failure", record_id=record_id, error=err_msg)
            results.append({"record_id": record_id, "error": err_msg})

    return results


@daft.udf(return_dtype=DataType.python())
def resolve_entities_udf(
    inference_results: Series,
    tenant_ids: Series,
    *,
    postgres_config: Any,
    modality: str = "face",
    threshold: float = 0.8,
) -> list[list[dict[str, Any]]]:
    """Queries VectorStore embeddings concurrently per partition with strict tenant isolation rules."""
    from galadril_vision.common.types import EmbeddingModality

    async def _resolve_item(item: dict, tenant_id_val: str, vector_store: Any) -> None:
        vector = item.get("embedding")
        if vector is not None:
            vector = _pad_embedding_if_needed(vector, expected_dim=1024)
            item["embedding"] = vector

            matches = await vector_store.find_similar(
                embedding=vector,
                modality=EmbeddingModality(modality),
                tenant_id=tenant_id_val,
                top_k=1,
            )
            if matches and matches[0][1] >= threshold:
                item["resolved_entity_id"] = matches[0][0]
                item["is_unknown"] = False
            else:
                item["resolved_entity_id"] = f"unknown_{modality}_{uuid4().hex}"
                item["is_unknown"] = True

    async def _resolve_batch(results_list: list, t_ids_list: list) -> list[list[dict[str, Any]]]:
        _, vector_store, _ = await _get_pg_stores(postgres_config)
        resolved_batch = []
        tasks = []

        for inference_data, tenant_id in zip(results_list, t_ids_list):
            if not inference_data or inference_data.get("error"):
                resolved_batch.append([])
                continue

            tenant_id_val = tenant_id or "acme"
            prediction = inference_data.get("prediction") or {}
            items = prediction.get("faces", [])
            
            for item in items:
                tasks.append(_resolve_item(item, tenant_id_val, vector_store))
            
            resolved_batch.append(items)

        if tasks:
            await asyncio.gather(*tasks)
        return resolved_batch

    try:
        return _run_async_blocking(_resolve_batch(inference_results.to_pylist(), tenant_ids.to_pylist()))
    except Exception:
        logger.error("resolve_entities_udf_failed", error=traceback.format_exc())
        raise


@daft.udf(return_dtype=DataType.bool())
def sink_to_db_udf(
    resolved_items_series: Series,
    record_ids: Series,
    sources: Series,
    tenant_ids: Series,
    event_types: Series,
    raw_payloads: Series,
    *,
    postgres_config: Any,
    entity_type: str = "PERSON",
    modality: str = "face",
) -> list[bool]:
    """Persists events, vertices, security permissions, and transactional embedding blocks concurrently."""
    from galadril_vision.common.types import (
        EmbeddingModality,
        EntityEmbedding,
        EntityStateRecord,
        EventRecord,
        EventType,
        GraphVertex,
        GraphEdge,
    )

    async def _sink_batch(items_list, rec_ids, srcs, t_ids, e_types, payloads) -> list[bool]:
        pg_client, vector_store, graph_store = await _get_pg_stores(postgres_config)
        all_states = []
        all_embeddings = []
        tenant_id_val = "acme"

        try:
            async with pg_client.connection() as conn:
                async with conn.transaction():
                    for (input_data, record_id, source, tenant_id, event_type, raw_payload) in zip(
                        items_list, rec_ids, srcs, t_ids, e_types, payloads
                    ):
                        if not input_data:
                            continue

                        tenant_id_val = tenant_id or "acme"
                        event = EventRecord(
                            event_id=f"evt_{record_id}",
                            tenant_id=tenant_id_val,
                            event_type=EventType.from_str(event_type) if event_type else EventType.OBSERVATION,
                            properties={"source": source or "unknown"},
                            timestamp=datetime.now(timezone.utc),
                        )
                        await graph_store.insert_event_on_connection(conn, event)

                        if isinstance(raw_payload, dict):
                            authz_block = raw_payload.get("authz")
                            if isinstance(authz_block, dict):
                                authz_tuples = authz_block.get("tuples")
                                if isinstance(authz_tuples, list) and authz_tuples:
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
                                            orjson.dumps(authz_tuples).decode("utf-8"),
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
                                    label=entity_type,
                                    tenant_id=tenant_id_val,
                                    properties={"is_unknown": item.get("is_unknown", True)},
                                ),
                            )
                            
                            await graph_store.create_edge_on_connection(
                                conn,
                                GraphEdge(
                                    source_vertex_id=entity_id,
                                    target_vertex_id=event.event_id,
                                    edge_type="APPEARS_IN",
                                    tenant_id=tenant_id_val,
                                    properties={},
                                )
                            )

                            all_states.append(
                                EntityStateRecord(
                                    entity_id=entity_id,
                                    event_id=event.event_id,
                                    state_type="sighting",
                                    state_value={"confidence": item.get("confidence", 0.0), "bbox": item.get("bbox")},
                                    event_time=event.timestamp,
                                    tenant_id=tenant_id_val,
                                )
                            )

                            if item.get("embedding") is not None:
                                vector_val = _pad_embedding_if_needed(item.get("embedding"), expected_dim=1024)
                                emb_record = EntityEmbedding(
                                    modality=EmbeddingModality(modality),
                                    vector=vector_val,
                                    metadata={"event_id": event.event_id},
                                    tenant_id=tenant_id_val,
                                )
                                all_embeddings.append((emb_record, entity_id))

                    batch_tasks = []
                    
                    if all_states:
                        if hasattr(graph_store, "insert_entity_states_batch_on_connection"):
                            batch_tasks.append(
                                graph_store.insert_entity_states_batch_on_connection(
                                    conn, all_states, expected_tenant_id=tenant_id_val
                                )
                            )
                        elif hasattr(graph_store, "insert_entity_states_batch"):
                            batch_tasks.append(graph_store.insert_entity_states_batch(all_states, connection=conn))
                        
                    if all_embeddings:
                        if hasattr(vector_store, "store_embeddings_batch_on_connection"):
                            batch_tasks.append(
                                vector_store.store_embeddings_batch_on_connection(
                                    conn, all_embeddings, expected_tenant_id=tenant_id_val
                                )
                            )
                        elif hasattr(vector_store, "store_embeddings_batch"):
                            batch_tasks.append(vector_store.store_embeddings_batch(all_embeddings, connection=conn))
                    
                    if batch_tasks:
                        await asyncio.gather(*batch_tasks)

            return [True] * len(items_list)

        except Exception as exc:
            logger.error("sink_batch_failed", error=str(exc))
            raise

    try:
        return _run_async_blocking(
            _sink_batch(
                resolved_items_series.to_pylist(),
                record_ids.to_pylist(),
                sources.to_pylist(),
                tenant_ids.to_pylist(),
                event_types.to_pylist(),
                raw_payloads.to_pylist(),
            )
        )
    except Exception:
        logger.error("sink_to_db_udf_failed", error=traceback.format_exc())
        raise
