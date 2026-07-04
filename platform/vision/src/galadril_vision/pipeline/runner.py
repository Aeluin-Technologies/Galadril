"""Pipeline orchestrator for consuming, staging, and dispatching Kafka record streams to S3."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from typing import Any, Dict, List

import structlog

from galadril_vision.connectors.kafka.consumer import (
    KafkaMultiTopicConsumer,
    IngestedMessage,
)
from galadril_vision.connectors.kafka.producer import KafkaJsonProducer
from galadril_vision.connectors.kafka.validator import (
    validate_and_normalize_kafka_batch,
)
from galadril_vision.pipeline.router import PipelineRouteKey
from galadril_vision.connectors.s3.transit import S3TransitService

logger = structlog.get_logger(__name__)


class VisionPipeline:
    """Consumes Kafka messages, offloads batches to S3 transit, and delegates compute to Dagster."""

    def __init__(
        self,
        *,
        consumer: KafkaMultiTopicConsumer,
        transit_service: S3TransitService,
        global_batch_timeout_s: float = 30.0,
        dlq_producer: KafkaJsonProducer | None = None,
        dlq_topic: str | None = None,
    ) -> None:
        self._consumer = consumer
        self._transit_service = transit_service
        self._global_timeout_s = global_batch_timeout_s
        self._dlq_producer = dlq_producer
        self._dlq_topic = dlq_topic

    async def process_batch(self, batch: list[IngestedMessage]) -> bool:
        """Partitions messages into transit S3 blocks and triggers downstream Dagster jobs.

        Returns:
            True if staging was successful allowing immediate Kafka offset commits.
        """
        start = time.perf_counter()
        validated_batch = validate_and_normalize_kafka_batch(batch)

        if validated_batch.rejected and self._dlq_producer and self._dlq_topic:
            for rejected_record in validated_batch.rejected:
                try:
                    await self._dlq_producer.produce_json(
                        topic=self._dlq_topic,
                        key="rejected",
                        payload={"rejected_record": str(rejected_record)},
                    )
                except Exception as dlq_err:
                    logger.error(
                        "dlq_produce_failed_for_rejected_record",
                        error=str(dlq_err),
                    )

        if not validated_batch.accepted:
            return True

        sub_batches: Dict[PipelineRouteKey, List[Dict[str, Any]]] = defaultdict(
            list
        )
        for record in validated_batch.accepted:
            rec_dict = record.model_dump()
            route_key = PipelineRouteKey(
                tenant_id=rec_dict.get("tenant_id", "UNKNOWN"),
                topic=rec_dict.get("topic", "raw"),
            )
            sub_batches[route_key].append(rec_dict)

        route_keys_ordered = list(sub_batches.keys())
        tasks = [
            self._dispatch_sub_batch(rk, sub_batches[rk])
            for rk in route_keys_ordered
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = True

        for rk, res in zip(route_keys_ordered, results):
            if isinstance(res, Exception) or res is False:
                logger.error(
                    "sub_batch_staging_or_dispatch_failed",
                    tenant_id=rk.tenant_id,
                    topic=rk.topic,
                    error=str(res),
                )
                success = False
                if self._dlq_producer and self._dlq_topic:
                    for rec in sub_batches[rk]:
                        try:
                            await self._dlq_producer.produce_json(
                                topic=self._dlq_topic,
                                key=rk.tenant_id,
                                payload=rec,
                            )
                        except Exception as dlq_err:
                            logger.error(
                                "dlq_produce_failed_for_failed_batch_record",
                                error=str(dlq_err),
                            )

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "batch_processed_dynamically",
            size=len(batch),
            elapsed_ms=round(elapsed_ms, 2),
            success=success,
        )
        return success

    async def _dispatch_sub_batch(
        self, route_key: PipelineRouteKey, records: list[dict[str, Any]]
    ) -> bool:
        """Stages records to S3 and triggers the Dagster processing pipeline execution context."""
        timestamp_slug = time.strftime("%Y%m%d-%H%M%S")
        batch_id = (
            f"{timestamp_slug}_{route_key.tenant_id}_{uuid.uuid4().hex[:6]}"
        )
        s3_key = f"batches/{route_key.tenant_id}/{batch_id}.parquet"

        try:
            await self._transit_service.upload_batch(
                key=s3_key, records=records, format_type="parquet"
            )
            return True
        except Exception as exc:
            logger.exception(
                "sub_batch_dispatch_critical_error",
                batch_id=batch_id,
                error=str(exc),
            )
            return False

    async def run(self, *, stop_event: asyncio.Event) -> None:
        """Polls messages continuously from the Kafka intake until a termination signal is set."""
        logger.info("vision_pipeline_started")

        while not stop_event.is_set():
            batch = await self._consumer.poll_batch()

            if not batch:
                await asyncio.sleep(0.05)
                continue

            logger.info("batch_polled", size=len(batch))
            if await self.process_batch(batch):
                await self._consumer.commit()
                logger.info("batch_committed", size=len(batch))
            else:
                logger.warning("batch_not_committed_due_to_failure")

        logger.info("vision_pipeline_stopped")
