"""Daft UDFs for modality-agnostic ESKG pipelines."""

from __future__ import annotations

import asyncio
import threading
import traceback
from typing import Any

import boto3
import cv2
import daft
import numpy as np
import structlog
from daft import DataType, Series

from galadril_inference.storage.s3 import S3Loader

from galadril_vision.pipeline.postgres_tasks import (
    PostgresRuntimeState,
    resolve_entities_batch,
    sink_to_db_batch,
)
from galadril_vision.pipeline.transform_helpers import (
    _build_raw_data_record,
    _decode_raw_content,
    _extract_text_payload,
    _infer_modality,
    _normalize_data_modality,
    _storage_location,
)
from galadril_vision.pipeline.worker_runtime import run_blocking

logger = structlog.get_logger(__name__)

_S3_CLIENT = None
_INFERENCE_ENGINES: dict[str, Any] = {}

_S3_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()


class CustomS3Loader(S3Loader):
    """Backward-compatible S3 loader that now inherits real upload support."""

    pass


def _get_postgres_state() -> PostgresRuntimeState:
    """Return a worker-local Postgres cache for async UDF bridges."""
    state = getattr(_THREAD_LOCAL, "postgres_state", None)
    if state is None:
        state = PostgresRuntimeState()
        _THREAD_LOCAL.postgres_state = state
    return state


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
    """Load raw records from inline payloads or S3 without assuming a media type."""
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
    """Run synchronous inference computations on local cached models per worker node."""
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
    """Resolve embeddings using a worker-local Postgres runtime."""

    inference_results_list = inference_results.to_pylist()
    tenant_ids_list = tenant_ids.to_pylist()
    timeout_s = max(
        float(getattr(postgres_config, "vector_search_timeout_ms", 5000))
        / 1000.0
        * 6.0,
        30.0,
    )

    async def _resolve() -> list[list[dict[str, Any]]]:
        logger.info(
            "before_resolve_entities",
            items=len(inference_results_list),
            timeout_s=round(timeout_s, 3),
        )
        try:
            result = await asyncio.wait_for(
                resolve_entities_batch(
                    state=_get_postgres_state(),
                    postgres_config=postgres_config,
                    inference_results=inference_results_list,
                    tenant_ids=tenant_ids_list,
                    modality=modality,
                    threshold=threshold,
                ),
                timeout=timeout_s,
            )
            logger.info(
                "after_resolve_entities",
                items=len(inference_results_list),
                timeout_s=round(timeout_s, 3),
            )
            return result
        except Exception:
            logger.error(
                "resolve_entities_batch_failed",
                error=traceback.format_exc(),
            )
            raise

    try:
        return run_blocking(_resolve())
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
    """Persist graph and vector mutations using the worker-local async runtime."""

    resolved_items_list = resolved_items_series.to_pylist()
    record_ids_list = record_ids.to_pylist()
    sources_list = sources.to_pylist()
    tenant_ids_list = tenant_ids.to_pylist()
    event_types_list = event_types.to_pylist()
    raw_payloads_list = raw_payloads.to_pylist()
    timeout_s = max(
        float(getattr(postgres_config, "vector_search_timeout_ms", 5000))
        / 1000.0
        * 6.0,
        30.0,
    )

    async def _sink() -> list[bool]:
        logger.info(
            "before_sink_to_db",
            items=len(resolved_items_list),
            timeout_s=round(timeout_s, 3),
        )
        try:
            result = await asyncio.wait_for(
                sink_to_db_batch(
                    state=_get_postgres_state(),
                    postgres_config=postgres_config,
                    resolved_items=resolved_items_list,
                    record_ids=record_ids_list,
                    sources=sources_list,
                    tenant_ids=tenant_ids_list,
                    event_types=event_types_list,
                    raw_payloads=raw_payloads_list,
                    entity_type=entity_type,
                    modality=modality,
                    edge_type=edge_type,
                    state_type=state_type,
                ),
                timeout=timeout_s,
            )
            logger.info(
                "after_sink_to_db",
                items=len(resolved_items_list),
                timeout_s=round(timeout_s, 3),
            )
            return result
        except Exception:
            logger.error(
                "sink_to_db_batch_failed", error=traceback.format_exc()
            )
            raise

    try:
        return run_blocking(_sink())
    except Exception:
        logger.error("sink_to_db_udf_failed", error=traceback.format_exc())
        raise
