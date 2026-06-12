"""Pipeline runtime orchestrator."""

from __future__ import annotations

import asyncio
import time

import structlog

from galadril_vision.connectors.kafka.consumer import KafkaMultiTopicConsumer, IngestedMessage
from galadril_vision.pipeline.executor import ESKGPipelineExecutor
from galadril_vision.connectors.kafka.validator import validate_and_normalize_kafka_batch

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
        self, batch: list[IngestedMessage]
    ) -> bool:
        """Process one batch.

        Returns:
          True if sinks completed successfully and it is safe to commit offsets.
          False if processing failed; offsets must not be committed (at-least-once).
        """
        start = time.perf_counter()

        validated_batch = validate_and_normalize_kafka_batch(batch)
        had_invalid_record = len(validated_batch.rejected) > 0

        if not validated_batch.accepted:
            return not had_invalid_record

        try:
            normalized_records = [
                record.model_dump() for record in validated_batch.accepted
            ]
            await self._executor.execute_batch(normalized_records)
        except Exception as exc:
            logger.error("executor_batch_failed", error=str(exc))
            return False
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "batch_processed",
                size=len(batch),
                elapsed_ms=round(elapsed_ms, 2),
            )

        return not had_invalid_record

    async def run(self, *, stop_event: asyncio.Event) -> None:
        """Main loop consuming Kafka."""
        logger.info("vision_pipeline_started")
        loop = asyncio.get_running_loop()

        while not stop_event.is_set():
            batch = await loop.run_in_executor(None, self._consumer.poll_batch)

            if not batch:
                await asyncio.sleep(0.05)
                continue

            ok = await self.process_batch(batch)
            if ok:
                await asyncio.to_thread(self._consumer.commit)
            else:
                logger.warning("batch_not_committed_due_to_failure")

        logger.info("vision_pipeline_stopped")
