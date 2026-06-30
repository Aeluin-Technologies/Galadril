"""Daft UDF for sinking resolved items to Postgres database."""

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
    sink_to_db_batch,
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


@daft.func.batch(return_dtype=DataType.bool())
@instrument("sink_to_db_batch")
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
    total_items = len(resolved_items_list)

    timeout_s = max(
        float(getattr(postgres_config, "vector_search_timeout_ms", 5000))
        / 1000.0
        * 6.0,
        30.0,
    )

    async def _sink() -> list[bool]:
        logger.info(
            "before_sink_to_db",
            items=total_items,
            entity_type=entity_type,
            modality=modality,
            edge_type=edge_type,
            timeout_s=round(timeout_s, 3),
        )
        try:
            start_time = time.perf_counter()
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
            duration = time.perf_counter() - start_time
            success_count = sum(1 for r in result if r)
            logger.info(
                "after_sink_to_db",
                items=total_items,
                successful_sinks=success_count,
                failed_sinks=total_items - success_count,
                duration_s=round(duration, 4),
            )
            return result
        except Exception:
            logger.exception("sink_to_db_batch_inner_async_failed")
            raise

    loop = getattr(_THREAD_LOCAL, "event_loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _THREAD_LOCAL.event_loop = loop

    try:
        return loop.run_until_complete(_sink())
    except Exception:
        logger.exception(
            "sink_to_db_udf_outer_execution_failed", batch_size=total_items
        )
        raise
