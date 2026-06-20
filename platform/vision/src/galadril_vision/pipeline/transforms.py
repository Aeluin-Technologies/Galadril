"""Daft UDFs for modality-agnostic ESKG pipelines."""

from __future__ import annotations

import asyncio
import os
import threading
import traceback
from pathlib import PurePosixPath
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


_EMBEDDING_KEYS = frozenset(("embedding", "embeddings", "vector", "features"))
_METADATA_KEYS = frozenset(
    (
        "bbox",
        "confidence",
        "label",
        "model_name",
        "model_version",
        "mime_type",
        "modality",
        "source_field",
        "raw_modality",
    )
)
_MODEL_ARTIFACT_EXTENSIONS = frozenset(
    ("bin", "joblib", "model", "onnx", "pkl", "pt", "pth", "safetensors")
)
_IMAGE_EXTENSIONS = frozenset(
    ("bmp", "jpeg", "jpg", "png", "tif", "tiff", "webp")
)
_AUDIO_EXTENSIONS = frozenset(
    ("aac", "flac", "m4a", "mp3", "ogg", "opus", "wav")
)
_VIDEO_EXTENSIONS = frozenset(
    ("avi", "m4v", "mkv", "mov", "mp4", "mpeg", "webm")
)
_TEXT_EXTENSIONS = frozenset(
    ("csv", "json", "jsonl", "log", "md", "txt", "xml", "yaml", "yml")
)
_DOCUMENT_EXTENSIONS = frozenset(
    ("doc", "docx", "html", "pdf", "ppt", "pptx", "rtf", "xls", "xlsx")
)
_TEXT_PAYLOAD_KEYS = (
    "content",
    "text",
    "body",
    "transcript",
    "caption",
    "description",
)


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
    models_bucket: str,
    models_prefix: str,
    endpoint_url: str | None,
) -> Any:
    """Initializes and caches the specific model inference engine instance."""
    global _INFERENCE_ENGINES
    if model_name not in _INFERENCE_ENGINES:
        with _INFERENCE_LOCK:
            if model_name not in _INFERENCE_ENGINES:
                from galadril_inference import InferenceEngine

                loader = CustomS3Loader(
                    bucket=models_bucket,
                    prefix=models_prefix,
                    endpoint_url=endpoint_url,
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
                from galadril_vision.connectors.postgres.client import (
                    PostgresClient,
                )
                from galadril_vision.connectors.postgres.graph import GraphStore
                from galadril_vision.connectors.postgres.vector import (
                    VectorStore,
                )

                postgres_config.min_connections = 1
                postgres_config.max_connections = 5

                client = PostgresClient(postgres_config)
                await client.connect()

                _VECTOR_STORE = VectorStore(client, postgres_config)
                _GRAPH_STORE = GraphStore(client, postgres_config)
                _PG_CLIENT = client
                logger.info("postgres_pool_initialized_on_worker")

    return _PG_CLIENT, _VECTOR_STORE, _GRAPH_STORE


def _pad_embedding_if_needed(
    vector: Any, expected_dim: int = 1024
) -> list[float] | None:
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
        padded = [float(value) for value in v_arr]
        padded.extend([0.0] * pad_size)
        return padded

    raise ValueError(
        f"Embedding dimension {current_dim} exceeds maximum allowed limit of {expected_dim}."
    )


def _get_vector_dimensions(postgres_config: Any) -> int:
    """Reads the configured vector dimension from Postgres connector settings."""
    raw_value = getattr(postgres_config, "vector_dimensions", 1024)
    try:
        dimensions = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("invalid_vector_dimensions_config", value=raw_value)
        dimensions = 1024
    return max(dimensions, 1)


def _get_param(params: Any, name: str, default: Any = None) -> Any:
    """Retrieves a pipeline parameter from either a Pydantic object or mapping."""
    if params is None:
        return default
    if isinstance(params, dict):
        return params.get(name, default)
    return getattr(params, name, default)


def _normalize_model_key(value: Any, default: str = "default") -> str:
    """Normalizes a model identifier into the vector-store partition key."""
    raw_value = value if isinstance(value, str) else default
    model_key = raw_value.strip().lower()
    if not model_key:
        model_key = default
    name = model_key.rsplit("/", 1)[-1]
    parts = name.rsplit(".", 1)
    if len(parts) == 2 and parts[1] in _MODEL_ARTIFACT_EXTENSIONS:
        return parts[0]
    return parts[-1]


def _normalize_data_modality(value: Any, default: str = "data") -> str:
    """Normalizes raw input modality names without constraining future domains."""
    raw_value = value if isinstance(value, str) else default
    modality = raw_value.strip().lower()
    return modality or default


def _infer_modality(
    storage_path: Any,
    raw_payload: Any,
    metadata: Any,
    default: str = "data",
) -> str:
    """Infers modality from explicit fields, MIME type, or object extension."""
    for container in (metadata, raw_payload):
        if not isinstance(container, dict):
            continue
        for key in (
            "modality",
            "input_type",
            "data_type",
            "media_type",
            "type",
        ):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return _normalize_data_modality(value)
        mime_type = container.get("mime_type") or container.get("content_type")
        if isinstance(mime_type, str) and "/" in mime_type:
            return _normalize_data_modality(mime_type.split("/", 1)[0])

    if isinstance(storage_path, str) and storage_path:
        suffix = PurePosixPath(storage_path).suffix.lower().lstrip(".")
        if suffix in _IMAGE_EXTENSIONS:
            return "image"
        if suffix in _AUDIO_EXTENSIONS:
            return "audio"
        if suffix in _VIDEO_EXTENSIONS:
            return "video"
        if suffix in _TEXT_EXTENSIONS:
            return "text"
        if suffix in _DOCUMENT_EXTENSIONS:
            return "document"

    return default


def _extract_text_payload(raw_payload: Any) -> str | None:
    """Returns inline text content without copying binary payload fields."""
    if not isinstance(raw_payload, dict):
        return None
    for key in _TEXT_PAYLOAD_KEYS:
        value = raw_payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _storage_location(
    storage_path: str, bucket: str, prefix: str
) -> tuple[str, str]:
    """Splits an absolute S3 URI or resolves a relative object key."""
    if storage_path.startswith("s3://"):
        parts = storage_path[5:].split("/", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""
    return bucket, f"{prefix}/{storage_path}".strip("/")


def _decode_raw_content(
    content: bytes,
    modality: str,
    mime_type: str | None,
    record_id: Any,
) -> Any:
    """Decodes only formats with a direct model-compatibility requirement."""
    if modality == "image" or (mime_type or "").startswith("image/"):
        nparr = np.frombuffer(content, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image is None:
            logger.warning("image_decode_failed", record_id=record_id)
        return cast(NDArray[np.uint8] | None, image)
    if modality == "text" or (mime_type or "").startswith("text/"):
        return content.decode("utf-8", errors="replace")
    return content


def _build_raw_data_record(
    *,
    record_id: Any,
    storage_path: Any,
    raw_payload: Any,
    metadata: Any,
    content: Any,
    modality: str,
    mime_type: str | None,
) -> dict[str, Any]:
    """Creates a stable payload envelope consumed by inference and sinks."""
    return {
        "record_id": record_id,
        "storage_path": storage_path,
        "modality": modality,
        "mime_type": mime_type,
        "data": content,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "raw_payload": raw_payload if isinstance(raw_payload, dict) else {},
    }


def _is_numeric_embedding(value: Any) -> bool:
    """Returns true when a value is a non-empty one-dimensional numeric vector."""
    if isinstance(value, np.ndarray):
        return value.ndim in (1, 2) and value.size > 0
    if not isinstance(value, (list, tuple)) or not value:
        return False
    return all(
        isinstance(item, (int, float, np.integer, np.floating))
        for item in value
    )


def _extract_embedding_items(
    prediction: Any, model_name: str
) -> list[dict[str, Any]]:
    """Extracts records containing embeddings from common and model-specific payloads."""
    model_key = _normalize_model_key(model_name)

    if not isinstance(prediction, dict):
        return []

    faces = prediction.get("faces")
    if isinstance(faces, list):
        items = [
            item
            for item in faces
            if isinstance(item, dict) and item.get("embedding") is not None
        ]
        if items:
            for item in items:
                item.setdefault("model_name", model_key)
            return items

    extracted: list[dict[str, Any]] = []

    def _walk(node: Any, inherited: dict[str, Any]) -> None:
        if isinstance(node, dict):
            local_metadata = {
                key: node[key]
                for key in _METADATA_KEYS
                if key in node and key != "model_name"
            }
            metadata = {**inherited, **local_metadata}
            metadata["model_name"] = _normalize_model_key(
                node.get("model_name")
                or inherited.get("model_name")
                or model_key
            )

            for key in _EMBEDDING_KEYS:
                value = node.get(key)
                if _is_numeric_embedding(value):
                    item = dict(metadata)
                    item["embedding"] = value
                    extracted.append(item)
                elif key == "embeddings" and isinstance(value, list):
                    for embedding in value:
                        if _is_numeric_embedding(embedding):
                            item = dict(metadata)
                            item["embedding"] = embedding
                            extracted.append(item)

            next_metadata = {**inherited, **metadata}
            for key, value in node.items():
                if key not in _EMBEDDING_KEYS:
                    _walk(value, next_metadata)
        elif isinstance(node, list):
            if _is_numeric_embedding(node):
                return
            for value in node:
                _walk(value, inherited)

    _walk(prediction, {"model_name": model_key})
    return extracted


def _build_state_value(
    item: dict[str, Any],
    *,
    modality: str,
    model_name: str,
    event_id: str,
) -> dict[str, Any]:
    """Builds a sparse state document for any extracted entity type."""
    state_value: dict[str, Any] = {
        "modality": modality,
        "model_name": model_name,
        "event_id": event_id,
    }
    for key in (
        "confidence",
        "bbox",
        "label",
        "model_version",
        "mime_type",
        "raw_modality",
        "source_field",
        "is_unknown",
    ):
        value = item.get(key)
        if value is not None:
            state_value[key] = value
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and metadata:
        state_value["metadata"] = metadata
    return state_value


@daft.udf(return_dtype=DataType.python())
def download_data_udf(
    storage_paths: Series,
    record_ids: Series,
    raw_payloads: Series,
    metadata_series: Series,
    *,
    bucket: str,
    prefix: str,
    endpoint_url: str | None,
    region_name: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
) -> list[dict[str, Any] | None]:
    """Loads raw records from inline payloads or S3 without assuming a media type."""
    client = _get_s3_client(endpoint_url, region_name, access_key, secret_key)
    results: list[dict[str, Any] | None] = []

    for storage_path, record_id, raw_payload, metadata in zip(
        storage_paths, record_ids, raw_payloads, metadata_series
    ):
        modality = _infer_modality(storage_path, raw_payload, metadata)
        mime_type = None
        for container in (metadata, raw_payload):
            if isinstance(container, dict):
                mime_type = (
                    container.get("mime_type")
                    or container.get("content_type")
                    or mime_type
                )

        inline_text = _extract_text_payload(raw_payload)
        if inline_text is not None:
            results.append(
                _build_raw_data_record(
                    record_id=record_id,
                    storage_path=storage_path,
                    raw_payload=raw_payload,
                    metadata=metadata,
                    content=inline_text,
                    modality="text" if modality == "data" else modality,
                    mime_type=mime_type or "text/plain",
                )
            )
            continue

        if not storage_path:
            results.append(None)
            continue

        try:
            s3_bucket, key = _storage_location(
                str(storage_path), bucket, prefix
            )
            response = client.get_object(Bucket=s3_bucket, Key=key)
            response_mime = response.get("ContentType")
            effective_mime = str(response_mime) if response_mime else mime_type
            data = _decode_raw_content(
                response["Body"].read(), modality, effective_mime, record_id
            )
            results.append(
                _build_raw_data_record(
                    record_id=record_id,
                    storage_path=storage_path,
                    raw_payload=raw_payload,
                    metadata=metadata,
                    content=data,
                    modality=modality,
                    mime_type=effective_mime,
                )
            )
        except Exception as exc:
            logger.warning(
                "raw_data_load_failed", record_id=record_id, error=str(exc)
            )
            results.append(None)

    return results


download_images_udf = download_data_udf


@daft.udf(return_dtype=DataType.python())
def run_inference_udf(
    raw_items: Series,
    record_ids: Series,
    *,
    models_bucket: str,
    models_prefix: str,
    artifact_endpoint_url: str | None,
    model_name: str,
    action: str = "embed",
) -> list[dict[str, Any]]:
    """Runs synchronous inference computations on local cached models per worker node."""
    from galadril_inference import PredictionRequest

    init_error = None
    engine = None

    try:
        engine = _get_inference_engine(
            model_name,
            models_bucket,
            models_prefix,
            artifact_endpoint_url,
        )
    except Exception:
        init_error = traceback.format_exc()
        logger.error(
            "inference_engine_initialization_failed",
            model_name=model_name,
            error=init_error,
        )

    results: list[dict[str, Any]] = []

    for raw_item, record_id in zip(raw_items, record_ids):
        if init_error:
            results.append({"record_id": record_id, "error": init_error})
            continue

        if raw_item is None:
            results.append({"record_id": record_id, "error": "No raw data"})
            continue

        try:
            if isinstance(raw_item, dict):
                data = raw_item.get("data")
                modality = _normalize_data_modality(raw_item.get("modality"))
                features = {
                    "action": action,
                    "data": data,
                    "modality": modality,
                    "mime_type": raw_item.get("mime_type"),
                    "storage_path": raw_item.get("storage_path"),
                    "metadata": raw_item.get("metadata") or {},
                    "raw_payload": raw_item.get("raw_payload") or {},
                }
                if modality == "image":
                    features["image"] = data
                elif modality == "text":
                    features["text"] = data
                elif modality == "audio":
                    features["audio"] = data
                elif modality == "video":
                    features["video"] = data
            else:
                modality = (
                    "image" if isinstance(raw_item, np.ndarray) else "data"
                )
                features = {
                    "action": action,
                    "data": raw_item,
                    "modality": modality,
                }
                if modality == "image":
                    features["image"] = raw_item

            req = PredictionRequest(
                model_name=model_name,
                features=features,
            )
            result = engine.predict(req)
            results.append(
                {
                    "record_id": record_id,
                    "prediction": result.prediction,
                    "confidence": result.confidence,
                    "raw_modality": modality,
                    "model_name": model_name,
                    "model_version": result.model_version,
                    "error": None,
                }
            )
        except Exception:
            err_msg = traceback.format_exc()
            logger.error(
                "inference_fatal_failure", record_id=record_id, error=err_msg
            )
            results.append({"record_id": record_id, "error": err_msg})

    return results


@daft.udf(return_dtype=DataType.python())
def resolve_entities_udf(
    inference_results: Series,
    tenant_ids: Series,
    *,
    postgres_config: Any,
    modality: str = "face_recognition",
    threshold: float = 0.7,
) -> list[list[dict[str, Any]]]:
    """Queries VectorStore embeddings concurrently per partition with strict tenant isolation rules."""
    from galadril_vision.common.types import normalize_embedding_modality

    async def _resolve_item(
        item: dict, tenant_id_val: str, vector_store: Any
    ) -> None:
        vector = item.get("embedding")
        if vector is not None:
            expected_dim = _get_vector_dimensions(postgres_config)
            vector = _pad_embedding_if_needed(vector, expected_dim=expected_dim)
            item["embedding"] = vector
            modality_key = normalize_embedding_modality(
                item.get("model_name") or item.get("modality") or modality
            )
            item["model_name"] = modality_key
            item.setdefault("modality", modality_key)

            matches = await vector_store.find_similar(
                embedding=vector,
                modality=modality_key,
                tenant_id=tenant_id_val,
                top_k=1,
            )
            if matches and matches[0][1] >= threshold:
                item["resolved_entity_id"] = matches[0][0]
                item["is_unknown"] = False
            else:
                item["resolved_entity_id"] = (
                    f"unknown_{modality_key}_{uuid4().hex}"
                )
                item["is_unknown"] = True

    async def _resolve_batch(
        results_list: list, t_ids_list: list
    ) -> list[list[dict[str, Any]]]:
        _, vector_store, _ = await _get_pg_stores(postgres_config)
        resolved_batch = []
        tasks = []

        for inference_data, tenant_id in zip(results_list, t_ids_list):
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
                tasks.append(_resolve_item(item, tenant_id_val, vector_store))

            resolved_batch.append(items)

        if tasks:
            await asyncio.gather(*tasks)
        return resolved_batch

    try:
        return _run_async_blocking(
            _resolve_batch(
                inference_results.to_pylist(), tenant_ids.to_pylist()
            )
        )
    except Exception:
        logger.error(
            "resolve_entities_udf_failed", error=traceback.format_exc()
        )
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
    entity_type: str = "Entity",
    modality: str = "data",
    edge_type: str = "DERIVED_FROM",
    state_type: str = "observation",
) -> list[bool]:
    """Persists events, vertices, security permissions, and transactional embedding blocks concurrently."""
    from galadril_vision.common.types import (
        EntityEmbedding,
        EntityStateRecord,
        EventRecord,
        EventType,
        GraphVertex,
        GraphEdge,
        normalize_embedding_modality,
    )

    async def _sink_batch(
        items_list, rec_ids, srcs, t_ids, e_types, payloads
    ) -> list[bool]:
        pg_client, vector_store, graph_store = await _get_pg_stores(
            postgres_config
        )
        all_states = []
        all_embeddings = []
        tenant_id_val = "acme"

        try:
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
                            properties={
                                "source": source or "unknown",
                                "record_id": record_id,
                                "modality": modality,
                            },
                            timestamp=datetime.now(timezone.utc),
                        )
                        await graph_store.insert_event_on_connection(
                            conn, event
                        )

                        if isinstance(raw_payload, dict):
                            authz_block = raw_payload.get("authz")
                            if isinstance(authz_block, dict):
                                authz_tuples = authz_block.get("tuples")
                                if (
                                    isinstance(authz_tuples, list)
                                    and authz_tuples
                                ):
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
                                            orjson.dumps(authz_tuples).decode(
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
                                        "is_unknown": item.get(
                                            "is_unknown", True
                                        ),
                                        "modality": item.get("modality")
                                        or modality,
                                        "raw_modality": item.get(
                                            "raw_modality"
                                        ),
                                        "label": item.get("label"),
                                    },
                                ),
                            )

                            await graph_store.create_edge_on_connection(
                                conn,
                                GraphEdge(
                                    source_vertex_id=entity_id,
                                    target_vertex_id=event.event_id,
                                    edge_type=item.get("edge_type")
                                    or edge_type,
                                    tenant_id=tenant_id_val,
                                    properties={
                                        "modality": item.get("modality")
                                        or modality,
                                        "raw_modality": item.get(
                                            "raw_modality"
                                        ),
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
                                    state_type=item.get("state_type")
                                    or state_type,
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
                                emb_record = EntityEmbedding(
                                    modality=modality_key,
                                    vector=vector_val,
                                    metadata={
                                        "event_id": event.event_id,
                                        "state_type": item.get("state_type")
                                        or state_type,
                                        "entity_type": item.get("entity_type")
                                        or entity_type,
                                        "raw_modality": item.get(
                                            "raw_modality"
                                        ),
                                        "model_name": modality_key,
                                        "model_version": item.get(
                                            "model_version"
                                        ),
                                    },
                                    tenant_id=tenant_id_val,
                                )
                                all_embeddings.append((emb_record, entity_id))

                    batch_tasks = []

                    if all_states:
                        if hasattr(
                            graph_store,
                            "insert_entity_states_batch_on_connection",
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
                                graph_store.insert_entity_states_batch(
                                    all_states, connection=conn
                                )
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
                                vector_store.store_embeddings_batch(
                                    all_embeddings, connection=conn
                                )
                            )

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
