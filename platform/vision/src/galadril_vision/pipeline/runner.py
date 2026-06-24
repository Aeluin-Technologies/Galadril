"""Pipeline runtime orchestrator using multi-tenant batch splitting and DLQ integration."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Dict, List

import structlog

from galadril_vision.connectors.kafka.consumer import (
    KafkaMultiTopicConsumer,
    IngestedMessage,
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
    """Consumes from Kafka, partitions processing records, and routes failures to a DLQ.

    Attributes:
        _consumer: The Kafka consumer instance reading from the shared intake.
        _router: The multi-tenant pipeline configuration router.
        _global_timeout_s: The maximum duration allowed for a batch execution.
        _dlq_producer: The Kafka producer for routing failed records.
        _dlq_topic: The topic name for the Dead Letter Queue.
    """

    def __init__(
        self,
        *,
        consumer: KafkaMultiTopicConsumer,
        router: MultiTenantPipelineRouter,
        global_batch_timeout_s: float = 30.0,
        dlq_producer: Any = None,
        dlq_topic: str | None = None,
    ) -> None:
        """Initializes the VisionPipeline instance."""
        self._consumer = consumer
        self._router = router
        self._global_timeout_s = global_batch_timeout_s
        self._dlq_producer = dlq_producer
        self._dlq_topic = dlq_topic

    async def process_batch(self, batch: list[IngestedMessage]) -> bool:
        """Isolates tenant failures."""
        start = time.perf_counter()

        validated_batch = validate_and_normalize_kafka_batch(batch)

        if validated_batch.rejected:
            logger.error(
                "invalid_records_detected_routing_to_dlq",
                count=len(validated_batch.rejected),
            )
            if self._dlq_producer and self._dlq_topic:
                for rejected_record in validated_batch.rejected:
                    try:
                        self._dlq_producer.produce(
                            self._dlq_topic,
                            value={"rejected_record": str(rejected_record)},
                        )
                    except Exception as dlq_err:
                        logger.error(
                            "dlq_produce_failed_for_rejected_record",
                            error=str(dlq_err),
                        )

        if not validated_batch.accepted:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "batch_processed_dynamically",
                size=len(batch),
                elapsed_ms=round(elapsed_ms, 2),
                success=True,
            )
            return True

        sub_batches: Dict[PipelineRouteKey, List[Dict[str, Any]]] = defaultdict(
            list
        )

        for record in validated_batch.accepted:
            rec_dict = record.model_dump()

            tenant_id = rec_dict.get("tenant_id", "UNKNOWN")
            topic = rec_dict.get("topic", "raw")
            route_key = PipelineRouteKey(tenant_id=tenant_id, topic=topic)
            sub_batches[route_key].append(rec_dict)

        route_keys_ordered: List[PipelineRouteKey] = list(sub_batches.keys())
        tasks = [
            self._dispatch_with_timeout(rk, sub_batches[rk])
            for rk in route_keys_ordered
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        success = True
        for rk, res in zip(route_keys_ordered, results):
            if isinstance(res, Exception):
                logger.error(
                    "tenant_dynamic_sub_batch_execution_failed",
                    tenant_id=rk.tenant_id,
                    topic=rk.topic,
                    error=str(res),
                    exc_info=res,
                )

                if self._dlq_producer and self._dlq_topic:
                    for rec in sub_batches[rk]:
                        try:
                            self._dlq_producer.produce(
                                self._dlq_topic, value=rec
                            )
                        except Exception as dlq_err:
                            logger.error(
                                "dlq_produce_failed", error=str(dlq_err)
                            )
                            success = False
                else:
                    success = False

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "batch_processed_dynamically",
            size=len(batch),
            elapsed_ms=round(elapsed_ms, 2),
            success=success,
        )

        return success

    async def _dispatch_with_timeout(
        self, route_key: PipelineRouteKey, records: list[dict[str, Any]]
    ) -> None:
        """Executes targeted pipeline steps, passing the global timeout as a fallback constraint.

        Args:
            route_key: The composite key routing the batch to the correct tenant pipeline.
            records: The data payload targeted for execution.
        """
        await self._router.dispatch_batch(
            route_key, records, fallback_timeout_s=self._global_timeout_s
        )

    async def run(self, *, stop_event: asyncio.Event) -> None:
        """Main loop consuming messages from Kafka until the stop event is triggered.

        Args:
            stop_event: The asyncio Event indicating graceful shutdown.
        """
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
