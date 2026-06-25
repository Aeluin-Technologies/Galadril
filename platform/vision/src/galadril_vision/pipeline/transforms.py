"""Daft UDFs for modality-agnostic ESKG pipelines."""

from __future__ import annotations

import asyncio
import threading
import traceback
from typing import Any, Optional

import daft
import numpy as np
import structlog
from daft import DataType, Series

from galadril_inference.storage.s3 import S3Loader
from galadril_vision.connectors.s3.client import S3Client
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

logger = structlog.get_logger(__name__)

_S3_CLIENT: Optional[S3Client] = None
_INFERENCE_ENGINES: dict[str, Any] = {}

_S3_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()


class CustomS3Loader(S3Loader):
    """Backward-compatible S3 loader that inherits real upload support."""

    pass


def _get_postgres_state() -> PostgresRuntimeState:
    """Return a worker-local Postgres cache for async UDF bridges."""
    state = getattr(_THREAD_LOCAL, "postgres_state", None)
    if state is None:
        state = PostgresRuntimeState()
        _THREAD_LOCAL.postgres_state = state
    return state


def _get_async_s3_client(
    bucket: str,
    endpoint_url: str | None,
    region_name: str | None,
    access_key: str | None,
    secret_key: str | None,
) -> S3Client:
    """Initializes and caches the thread-safe async S3Client connector singleton."""
    global _S3_CLIENT
    if _S3_CLIENT is None:
        with _S3_LOCK:
            if _S3_CLIENT is None:
                _S3_CLIENT = S3Client(
                    bucket=bucket,
                    endpoint_url=endpoint_url,
                    aws_access_key=access_key,
                    aws_secret_key=secret_key,
                    aws_region=region_name or "us-east-1",
                )
    return _S3_CLIENT


async def _get_inference_engine(
    model_name: str,
    models_bucket: str,
    models_prefix: str,
    endpoint_url: str | None,
) -> Any:
    """Initializes and caches the specific model inference engine instance."""
    global _INFERENCE_ENGINES
    if model_name not in _INFERENCE_ENGINES:
        from galadril_inference import InferenceEngine

        loader = CustomS3Loader(
            bucket=models_bucket,
            prefix=models_prefix,
            endpoint_url=endpoint_url,
        )
        engine = InferenceEngine(loader=loader)
        await engine.load_model(model_name)
        _INFERENCE_ENGINES[model_name] = engine
        logger.info("model_loaded_on_worker", model=model_name)
    return _INFERENCE_ENGINES[model_name]


@daft.func.batch(return_dtype=DataType.python())
async def download_data_udf(
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
    """Load records concurrently from inline payloads or S3 without blocking worker nodes."""

    async def _download_single(
        storage_path: Any,
        record_id: Any,
        raw_payload: Any,
        metadata: Any,
        client: S3Client,
    ) -> dict[str, Any] | None:
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
            return _build_raw_data_record(
                record_id=record_id,
                storage_path=storage_path,
                raw_payload=raw_payload,
                metadata=metadata,
                content=inline_text,
                modality="text" if modality == "data" else modality,
                mime_type=mime_type or "text/plain",
            )

        if not storage_path:
            return None

        try:
            s3_bucket, key = _storage_location(
                str(storage_path), bucket, prefix
            )
            content, effective_mime = await client.get_object_with_metadata(
                key, target_bucket=s3_bucket
            )
            effective_mime = effective_mime or mime_type

            data = _decode_raw_content(
                content, modality, effective_mime, record_id
            )
            return _build_raw_data_record(
                record_id=record_id,
                storage_path=storage_path,
                raw_payload=raw_payload,
                metadata=metadata,
                content=data,
                modality=modality,
                mime_type=effective_mime,
            )
        except Exception as exc:
            logger.warning(
                "raw_data_load_failed", record_id=record_id, error=str(exc)
            )
            return None

    async with S3Client(
        bucket=bucket,
        endpoint_url=endpoint_url,
        aws_access_key=access_key,
        aws_secret_key=secret_key,
        aws_region=region_name or "us-east-1",
    ) as client:
        tasks = [
            _download_single(sp, rid, rp, meta, client)
            for sp, rid, rp, meta in zip(
                storage_paths, record_ids, raw_payloads, metadata_series
            )
        ]
        return list(await asyncio.gather(*tasks))


@daft.func.batch(return_dtype=DataType.python())
async def run_inference_udf(
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
        engine = await _get_inference_engine(
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
                if modality in ("image", "text", "audio", "video"):
                    features[modality] = data
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

            req = PredictionRequest(model_name=model_name, features=features)
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


@daft.func.batch(return_dtype=DataType.python())
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
                "resolve_entities_batch_failed", error=traceback.format_exc()
            )
            raise

    loop = getattr(_THREAD_LOCAL, "event_loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _THREAD_LOCAL.event_loop = loop

    try:
        return loop.run_until_complete(_resolve())
    except Exception:
        logger.error(
            "resolve_entities_udf_failed", error=traceback.format_exc()
        )
        raise


@daft.func.batch(return_dtype=DataType.bool())
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

    loop = getattr(_THREAD_LOCAL, "event_loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _THREAD_LOCAL.event_loop = loop

    try:
        return loop.run_until_complete(_sink())
    except Exception:
        logger.error("sink_to_db_udf_failed", error=traceback.format_exc())
        raise
