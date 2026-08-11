"""Actor-local inference engine initialization without dataframe runtimes."""

from __future__ import annotations

import asyncio
import time

import structlog
from galadril_inference.core.engine import InferenceEngine
from galadril_inference.storage.s3 import S3Loader

logger = structlog.get_logger(__name__)

type EngineKey = tuple[str, str, str, str | None]
type EngineEntry = InferenceEngine | asyncio.Task[InferenceEngine]
_INFERENCE_ENGINES: dict[EngineKey, EngineEntry] = {}


async def get_inference_engine(
    *,
    model_name: str,
    models_bucket: str,
    models_prefix: str,
    endpoint_url: str | None,
) -> InferenceEngine:
    """Loads each model/storage tuple once in the long-lived Ray process."""
    key = (model_name, models_bucket, models_prefix, endpoint_url)
    cached = _INFERENCE_ENGINES.get(key)
    if cached is None:
        task = asyncio.create_task(
            _load_inference_engine(
                model_name=model_name,
                models_bucket=models_bucket,
                models_prefix=models_prefix,
                endpoint_url=endpoint_url,
            ),
            name=f"load-model:{model_name}",
        )
        _INFERENCE_ENGINES[key] = task
        cached = task

    if isinstance(cached, asyncio.Task):
        try:
            engine = await cached
        except Exception:
            if _INFERENCE_ENGINES.get(key) is cached:
                _INFERENCE_ENGINES.pop(key, None)
            raise
        _INFERENCE_ENGINES[key] = engine
        return engine
    return cached


async def _load_inference_engine(
    *,
    model_name: str,
    models_bucket: str,
    models_prefix: str,
    endpoint_url: str | None,
) -> InferenceEngine:
    """Creates an engine and resolves its model artifact asynchronously."""
    started_at = time.perf_counter()
    loader = S3Loader(
        bucket=models_bucket,
        prefix=models_prefix,
        endpoint_url=endpoint_url,
    )
    engine = InferenceEngine(loader=loader)
    await engine.load_model(model_name)
    logger.info(
        "ray_actor_model_loaded",
        model=model_name,
        duration_seconds=time.perf_counter() - started_at,
    )
    return engine
