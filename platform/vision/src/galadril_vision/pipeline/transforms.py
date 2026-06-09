"""Daft UDFs for the vision pipeline."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, cast
from uuid import uuid4
from datetime import datetime, timezone

import daft
import numpy as np
import structlog
from daft import DataType, Series
from numpy.typing import NDArray

logger = structlog.get_logger(__name__)

_S3_CLIENT = None
_INFERENCE_ENGINES: dict[str, Any] = {}
_PG_CLIENT = None
_VECTOR_STORE = None
_GRAPH_STORE = None

# Threading locks to secure shared resources against concurrent worker boots
_S3_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_PG_THREAD_LOCK = threading.Lock()


def _get_s3_client(endpoint_url: str | None) -> Any:
    global _S3_CLIENT
    if _S3_CLIENT is None:
        with _S3_LOCK:
            if _S3_CLIENT is None:
                import boto3

                _S3_CLIENT = boto3.client(
                    "s3", region_name="eu-west-1", endpoint_url=endpoint_url
                )
                logger.info("s3_client_initialized_on_worker")
    return _S3_CLIENT


def _get_inference_engine(
    model_name: str, bucket: str, prefix: str, endpoint_url: str | None
) -> Any:
    global _INFERENCE_ENGINES
    if model_name not in _INFERENCE_ENGINES:
        with _INFERENCE_LOCK:
            if model_name not in _INFERENCE_ENGINES:
                from galadril_inference import InferenceEngine
                from galadril_inference.storage import S3Loader

                loader = S3Loader(
                    bucket=bucket, prefix=prefix, endpoint_url=endpoint_url
                )
                engine = InferenceEngine(loader=loader)
                engine.load_model(model_name)
                _INFERENCE_ENGINES[model_name] = engine
                logger.info("model_loaded_on_worker", model=model_name)
    return _INFERENCE_ENGINES[model_name]


async def _get_pg_stores(dsn: str) -> tuple[Any, Any, Any]:
    global _PG_CLIENT, _VECTOR_STORE, _GRAPH_STORE
    if _PG_CLIENT is None:
        with _PG_THREAD_LOCK:
            if _PG_CLIENT is None:
                from galadril_vision.common.config import PostgresConfig
                from galadril_vision.connectors.postgres.client import (
                    PostgresClient,
                )
                from galadril_vision.connectors.postgres.vector import (
                    VectorStore,
                )
                from galadril_vision.connectors.postgres.graph import GraphStore

                # Create a pool per worker to prevent max_connections exhaustion.
                config = PostgresConfig(
                    dsn=dsn, min_connections=1, max_connections=5
                )
                client = PostgresClient(config)
                await client.connect()

                _VECTOR_STORE = VectorStore(client, config)
                _GRAPH_STORE = GraphStore(client, config)
                _PG_CLIENT = client
                logger.info("postgres_pool_initialized_on_worker")

    return _PG_CLIENT, _VECTOR_STORE, _GRAPH_STORE


@daft.udf(return_dtype=DataType.python())
def download_images_udf(
    storage_paths: Series,
    record_ids: Series,
    *,
    bucket: str,
    prefix: str,
    endpoint_url: str | None,
) -> list[NDArray[np.uint8] | None]:
    """Download raw images from the image store (S3) across Ray workers."""
    import cv2

    client = _get_s3_client(endpoint_url)
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
            logger.warning(
                "image_download_failed", record_id=record_id, error=str(exc)
            )
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
) -> list[dict[str, Any]]:
    """Generic inference UDF running on Ray workers. Models are cached per worker."""
    from galadril_inference import PredictionRequest

    engine = _get_inference_engine(
        model_name, artifact_bucket, artifact_prefix, artifact_endpoint_url
    )
    results: list[dict[str, Any]] = []

    for image, record_id in zip(images, record_ids):
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
        except Exception as exc:
            logger.warning(
                "inference_failed", record_id=record_id, error=str(exc)
            )
            results.append({"record_id": record_id, "error": str(exc)})

    return results


@daft.udf(return_dtype=DataType.python())
def resolve_entities_udf(
    inference_results: Series,
    tenant_ids: Series,
    *,
    postgres_dsn: str,
    modality: str = "face",
    threshold: float = 0.8,
) -> list[list[dict[str, Any]]]:
    """Resolve entities against the vector store locally on the Ray worker with tenant isolation."""
    from galadril_vision.common.types import EmbeddingModality

    async def _resolve_batch(results, t_ids) -> list[list[dict[str, Any]]]:
        _, vector_store, _ = await _get_pg_stores(postgres_dsn)
        resolved_batch = []

        for inference_data, tenant_id in zip(results, t_ids):
            if not inference_data or inference_data.get("error"):
                resolved_batch.append([])
                continue

            tenant_id_val = tenant_id or "acme"
            items = inference_data.get("prediction", {}).get("faces", [])
            for item in items:
                vector = item.get("embedding")
                if vector:
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
                        item["resolved_entity_id"] = (
                            f"unknown_{modality}_{uuid4().hex}"
                        )
                        item["is_unknown"] = True
            resolved_batch.append(items)

        return resolved_batch

    return asyncio.run(_resolve_batch(inference_results, tenant_ids))


@daft.udf(return_dtype=DataType.bool())
def sink_to_db_udf(
    resolved_items_series: Series,
    record_ids: Series,
    sources: Series,
    tenant_ids: Series,
    event_types: Series,
    raw_payloads: Series,
    *,
    postgres_dsn: str,
    entity_type: str = "PERSON",
    modality: str = "face",
) -> list[bool]:
    """Sinks nodes, states, vectors, and security permission boundaries concurrently on Ray."""
    from galadril_vision.common.types import (
        EmbeddingModality,
        EntityEmbedding,
        EntityStateRecord,
        EventRecord,
        EventType,
        GraphVertex,
    )

    async def _sink_batch(
        items_list, rec_ids, srcs, t_ids, e_types, payloads
    ) -> list[bool]:
        pg_client, vector_store, graph_store = await _get_pg_stores(
            postgres_dsn
        )

        all_states = []
        all_embeddings = []

        try:
            # Bind all database writes inside a single transaction per micro-batch
            async with pg_client.connection() as conn:
                async with conn.transaction():
                    for (
                        input_data,
                        record_id,
                        source,
                        tenant_id,
                        event_type,
                        raw_payload,
                    ) in zip(
                        items_list, rec_ids, srcs, t_ids, e_types, payloads
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
                            properties={"source": source or "unknown"},
                            timestamp=datetime.now(timezone.utc),
                        )
                        await graph_store.insert_event(event, connection=conn)

                        if isinstance(raw_payload, dict):
                            authz_block = raw_payload.get("authz")
                            if isinstance(authz_block, dict):
                                authz_tuples = authz_block.get("tuples")
                                if (
                                    isinstance(authz_tuples, list)
                                    and authz_tuples
                                ):
                                    import orjson

                                    # Reuse current transaction context connection
                                    await conn.execute(
                                        """
                                        INSERT INTO authz_outbox (
                                            tenant_id, 
                                            object_id, 
                                            tuples_json, 
                                            attempts, 
                                            next_retry_at, 
                                            created_at, 
                                            updated_at
                                        ) VALUES ($1, $2, $3, 0, NOW(), NOW(), NOW())
                                        """,
                                        (
                                            tenant_id_val,
                                            str(record_id),
                                            orjson.dumps(authz_tuples).decode(
                                                "utf-8"
                                            ),
                                        ),
                                    )

                        for item in input_data:
                            entity_id = item.get("resolved_entity_id")
                            if not entity_id:
                                continue

                            await graph_store.ensure_vertex(
                                GraphVertex(
                                    vertex_id=entity_id,
                                    label=entity_type,
                                    tenant_id=tenant_id_val,
                                    properties={
                                        "is_unknown": item.get(
                                            "is_unknown", True
                                        )
                                    },
                                ),
                                connection=conn,
                            )
                            await graph_store.link_entity_to_event(
                                entity_id=entity_id,
                                event_id=event.event_id,
                                role="APPEARS_IN",
                                tenant_id=tenant_id_val,
                                connection=conn,
                            )

                            all_states.append(
                                EntityStateRecord(
                                    entity_id=entity_id,
                                    event_id=event.event_id,
                                    state_type="sighting",
                                    state_value={
                                        "confidence": item.get(
                                            "confidence", 0.0
                                        ),
                                        "bbox": item.get("bbox"),
                                    },
                                    event_time=event.timestamp,
                                    tenant_id=tenant_id_val,
                                )
                            )

                            if item.get("embedding"):
                                emb_record = EntityEmbedding(
                                    modality=EmbeddingModality(modality),
                                    vector=item.get("embedding"),
                                    metadata={"event_id": event.event_id},
                                    tenant_id=tenant_id_val,
                                )
                                all_embeddings.append((emb_record, entity_id))

                    if all_states and hasattr(
                        graph_store, "insert_entity_states_batch"
                    ):
                        await graph_store.insert_entity_states_batch(
                            all_states, connection=conn
                        )
                    if all_embeddings and hasattr(
                        vector_store, "store_embeddings_batch"
                    ):
                        await vector_store.store_embeddings_batch(
                            all_embeddings, connection=conn
                        )

            return [True] * len(items_list)

        except Exception as exc:
            # Intentionally propagate the error up to block Kafka offset commits
            logger.error("sink_batch_failed", error=str(exc))
            raise

    return asyncio.run(
        _sink_batch(
            resolved_items_series,
            record_ids,
            sources,
            tenant_ids,
            event_types,
            raw_payloads,
        )
    )
