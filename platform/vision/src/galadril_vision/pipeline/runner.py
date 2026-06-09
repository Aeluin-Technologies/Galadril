"""Pipeline runtime orchestrator."""

from __future__ import annotations

import asyncio
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
    ) -> bool:
        """Process one batch.

        Returns:
          True if sinks completed successfully and it is safe to commit offsets.
          False if processing failed; offsets must not be committed (at-least-once).
        """
        start = time.perf_counter()

        normalized_records: list[dict[str, Any]] = []
        had_invalid_record = False
        for topic, payload in batch:
            if not isinstance(payload, dict):
                had_invalid_record = True
                logger.warning(
                    "invalid_payload_type", type=type(payload), topic=topic
                )
                continue
            try:
                normalized_records.append(EventNormalizer.normalize(payload))
            except Exception as exc:
                had_invalid_record = True
                logger.error(
                    "kafka_normalization_failed",
                    topic=topic,
                    error=str(exc),
                    raw_payload=payload,
                )

        if not normalized_records:
            return not had_invalid_record

        try:
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
