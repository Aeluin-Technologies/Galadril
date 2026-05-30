"""Pipeline runtime orchestrator (no bootstrapping)."""

from __future__ import annotations

import time
from typing import Any

import structlog

from galadril_vision.connectors.kafka.consumer import KafkaMultiTopicConsumer
from galadril_vision.connectors.kafka.schemas import EventNormalizer
from galadril_vision.pipeline.executor import ESKGPipelineExecutor

logger = structlog.get_logger(__name__)


class VisionPipeline:
    """Consumes Kafka, normalizes messages, and executes the pipeline."""

    def __init__(
        self,
        *,
        consumer: KafkaMultiTopicConsumer,
        executor: ESKGPipelineExecutor,
    ) -> None:
        self._consumer = consumer
        self._executor = executor

    async def process_batch(
        self, batch: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """Normalize a batch from Kafka and delegate execution to the executor."""
        start = time.perf_counter()

        normalized_records: list[dict[str, Any]] = []
        for _topic, payload in batch:
            if not isinstance(payload, dict):
                logger.warning("invalid_payload", type=type(payload))
                continue
            normalized_records.append(EventNormalizer.normalize(payload))

        if normalized_records:
            try:
                await self._executor.execute_batch(normalized_records)
            except Exception as exc:
                logger.error("executor_batch_failed", error=str(exc))

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "batch_processed",
            size=len(batch),
            elapsed_ms=round(elapsed_ms, 2),
        )

    async def run(self) -> None:
        """Main loop consuming Kafka."""
        logger.info("vision_pipeline_started")

        try:
            for batch in self._consumer.stream():
                await self.process_batch(batch)
                self._consumer.commit()
        except KeyboardInterrupt:
            logger.info("pipeline_interrupted")

        logger.info("vision_pipeline_stopped")
