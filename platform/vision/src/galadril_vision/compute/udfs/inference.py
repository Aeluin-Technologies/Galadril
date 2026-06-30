"""Daft UDF for running model inference batches."""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any

import daft
import numpy as np
import structlog
from daft import DataType, Series

from galadril_inference.core.engine import InferenceEngine
from galadril_inference.storage.s3 import S3Loader
from galadril_vision.compute.helpers import _normalize_data_modality
from galadril_vision.telemetry.tracing import instrument

logger = structlog.get_logger(__name__)

_INFERENCE_ENGINES: dict[
    str, InferenceEngine | asyncio.Task[InferenceEngine]
] = {}


class CustomS3Loader(S3Loader):
    """Backward-compatible S3 loader that inherits real upload support."""

    pass


async def _get_inference_engine(
    model_name: str,
    models_bucket: str,
    models_prefix: str,
    endpoint_url: str | None,
) -> InferenceEngine:
    """Initializes and caches the specific model inference engine instance."""
    global _INFERENCE_ENGINES
    if model_name not in _INFERENCE_ENGINES:
        logger.debug(
            "loading_inference_model_start",
            model=model_name,
            bucket=models_bucket,
        )
        start_time = time.perf_counter()

        async def _load() -> InferenceEngine:
            loader = CustomS3Loader(
                bucket=models_bucket,
                prefix=models_prefix,
                endpoint_url=endpoint_url,
            )
            engine = InferenceEngine(loader=loader)
            await engine.load_model(model_name)

            duration = time.perf_counter() - start_time
            logger.info(
                "model_loaded_on_worker",
                model=model_name,
                duration_s=round(duration, 4),
            )
            _INFERENCE_ENGINES[model_name] = engine
            return engine

        _INFERENCE_ENGINES[model_name] = asyncio.create_task(_load())

    res = _INFERENCE_ENGINES[model_name]
    if isinstance(res, asyncio.Task):
        return await res
    return res


@daft.func.batch(return_dtype=DataType.python())
@instrument("run_inference_batch")
async def run_inference_udf(
    raw_items: Series,
    record_ids: Series,
    *,
    model_name: str,
    models_bucket: str,
    models_prefix: str,
    artifact_endpoint_url: str | None = None,
    action: str = "embed",
) -> list[dict[str, Any]]:
    """Run synchronous inference computations on cached models without a stateful class wrapper."""
    from galadril_inference.common.types import PredictionRequest

    total_items = len(raw_items)
    logger.debug(
        "run_inference_batch_start",
        total_items=total_items,
        model=model_name,
        action=action,
    )

    try:
        engine = await _get_inference_engine(
            model_name=model_name,
            models_bucket=models_bucket,
            models_prefix=models_prefix,
            endpoint_url=artifact_endpoint_url,
        )
        if engine is None:
            raise RuntimeError("Inference Engine resolved to None.")
    except Exception as e:
        logger.exception(
            "critical_inference_engine_init_failed",
            model_name=model_name,
        )
        raise RuntimeError(
            f"Critical: Failed to initialize Inference Engine for {model_name} during UDF execution."
        ) from e

    results: list[dict[str, Any]] = []
    processed_modalities: dict[str, int] = {}
    inference_failures = 0

    for raw_item, record_id in zip(raw_items, record_ids):
        if raw_item is None:
            logger.warning("inference_skipped_empty_item", record_id=record_id)
            results.append({"record_id": record_id, "error": "No raw data"})
            inference_failures += 1
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

            processed_modalities[modality] = (
                processed_modalities.get(modality, 0) + 1
            )

            logger.debug(
                "executing_model_prediction",
                record_id=record_id,
                model=model_name,
                modality=modality,
            )

            req = PredictionRequest(model_name=model_name, features=features)

            start_predict = time.perf_counter()
            result = engine.predict(req)
            predict_duration = time.perf_counter() - start_predict

            logger.debug(
                "prediction_computed",
                record_id=record_id,
                confidence=result.confidence,
                duration_s=round(predict_duration, 4),
            )

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
            inference_failures += 1
            err_msg = traceback.format_exc()
            logger.exception(
                "inference_item_runtime_failure",
                record_id=record_id,
                model_name=model_name,
            )
            results.append({"record_id": record_id, "error": err_msg})

    logger.info(
        "run_inference_batch_complete",
        total_items=total_items,
        failed_items=inference_failures,
        modalities_processed=processed_modalities,
        model=model_name,
    )
    return results
