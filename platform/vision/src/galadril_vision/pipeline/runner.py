"""Pipeline runtime orchestrator using multi-tenant batch splitting and dynamic discovery."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import time
from typing import Any, Dict, List
import structlog

from galadril_vision.connectors.kafka.consumer import (
    IngestedMessage,
    KafkaMultiTopicConsumer,
)
from galadril_vision.connectors.kafka.validator import (
    validate_and_normalize_kafka_batch,
)
from galadril_vision.pipeline.router import (
    MultiTenantPipelineRouter,
    PipelineRouteKey,
)

logger = structlog.get_logger(__name__)


class VisionPipeline:
    """Consumes from Kafka, partitions processing records by route signature keys, and runs engines."""

    def __init__(
        self,
        *,
        consumer: KafkaMultiTopicConsumer,
        router: MultiTenantPipelineRouter,
        global_batch_timeout_s: float = 30.0,
    ) -> None:
        self._consumer = consumer
        self._router = router
        self._global_timeout_s = global_batch_timeout_s

    async def process_batch(self, batch: list[IngestedMessage]) -> bool:
        """Groups accepted items along structural routing keys without hardcoded presets."""
        start = time.perf_counter()

        validated_batch = validate_and_normalize_kafka_batch(batch)
        had_invalid_record = len(validated_batch.rejected) > 0

        if not validated_batch.accepted:
            return not had_invalid_record

        sub_batches: Dict[PipelineRouteKey, List[Dict[str, Any]]] = defaultdict(
            list
        )

        for record in validated_batch.accepted:
            rec_dict = record.model_dump()

            tenant_id = rec_dict.get("tenant_id", "UNKNOWN")
            topic = rec_dict.get("source", "unknown")
            event_type = rec_dict.get("event_type", "UNKNOWN")
            route_key = PipelineRouteKey(
                tenant_id=tenant_id, topic=topic, event_type=event_type
            )
            sub_batches[route_key].append(rec_dict)

        tasks = []
        for route_key, records_chunk in sub_batches.items():
            tasks.append(self._dispatch_with_timeout(route_key, records_chunk))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        execution_success = True
        for res in results:
            if isinstance(res, Exception):
                logger.error(
                    "tenant_dynamic_sub_batch_execution_failed", error=str(res)
                )
                execution_success = False

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "batch_processed_dynamically",
            size=len(batch),
            elapsed_ms=round(elapsed_ms, 2),
            success=execution_success,
        )

        if not execution_success:
            return False

        return not had_invalid_record

    async def _dispatch_with_timeout(
        self, route_key: PipelineRouteKey, records: list[dict[str, Any]]
    ) -> None:
        """Executes targeted pipeline steps bounded by configured processing timeouts."""
        await asyncio.wait_for(
            self._router.dispatch_batch(route_key, records),
            timeout=self._global_timeout_s,
        )

    async def run(self, *, stop_event: asyncio.Event) -> None:
        """Main loop consuming Kafka (Restored for main.py integration)."""
        logger.info("vision_pipeline_started")
        loop = asyncio.get_running_loop()

        while not stop_event.is_set():
            batch = await loop.run_in_executor(None, self._consumer.poll_batch)

            if not batch:
                await asyncio.sleep(0.05)
                continue

            logger.info("batch_polled", size=len(batch))
            ok = await self.process_batch(batch)
            if ok:
                await asyncio.to_thread(self._consumer.commit)
                logger.info("batch_committed", size=len(batch))
            else:
                logger.warning("batch_not_committed_due_to_failure")

        logger.info("vision_pipeline_stopped")
