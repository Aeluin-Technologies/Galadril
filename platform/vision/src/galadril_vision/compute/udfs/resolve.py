"""Daft UDF for resolving entities via Postgres vector search."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import daft
import structlog
from daft import DataType, Series

from galadril_vision.compute.tasks import (
    PostgresRuntimeState,
    resolve_entities_batch,
)
from galadril_vision.telemetry.tracing import instrument

logger = structlog.get_logger(__name__)

_THREAD_LOCAL = threading.local()


def _get_postgres_state() -> PostgresRuntimeState:
    """Return a worker-local Postgres cache for async UDF bridges."""
    state = getattr(_THREAD_LOCAL, "postgres_state", None)
    if state is None:
        state = PostgresRuntimeState()
        _THREAD_LOCAL.postgres_state = state
    return state


@daft.func.batch(return_dtype=DataType.python())
@instrument("resolve_entities_batch")
def resolve_entities_udf(
    inference_results: Series,
    tenant_ids: Series,
    *,
    postgres_config: Any,
    modality: str = "face_recognition",
    threshold: float = 0.7,
) -> list[list[dict[str, Any]]]:
    """Resolve embeddings using a worker-local Postgres runtime cache framework."""

    inference_results_list = inference_results.to_pylist()
    tenant_ids_list = tenant_ids.to_pylist()
    total_items = len(inference_results_list)

    timeout_s = max(
        float(getattr(postgres_config, "vector_search_timeout_ms", 5000))
        / 1000.0
        * 6.0,
        30.0,
    )

    async def _resolve() -> list[list[dict[str, Any]]]:
        logger.info(
            "before_resolve_entities",
            items=total_items,
            modality=modality,
            threshold=threshold,
            timeout_s=round(timeout_s, 3),
        )
        try:
            start_time = time.perf_counter()
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
            duration = time.perf_counter() - start_time
            logger.info(
                "after_resolve_entities",
                items=total_items,
                resolved_count=len(result),
                duration_s=round(duration, 4),
            )
            return result
        except Exception:
            logger.exception("resolve_entities_batch_inner_async_failed")
            raise

    loop = getattr(_THREAD_LOCAL, "event_loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _THREAD_LOCAL.event_loop = loop

    try:
        return loop.run_until_complete(_resolve())
    except Exception:
        logger.exception(
            "resolve_entities_udf_outer_execution_failed",
            batch_size=total_items,
        )
        raise
