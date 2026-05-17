"""Pipeline orchestrator driven by galadril-pipeline DAG."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from galadril_vision.connectors.kafka.consumer import KafkaMultiTopicConsumer
from galadril_vision.connectors.kafka.schemas import EventNormalizer
from galadril_vision.pipeline.executor import ESKGPipelineExecutor

if TYPE_CHECKING:
    from galadril_vision.common.config import VisionConfig
    from galadril_pipeline.graph import PipelineGraph

logger = structlog.get_logger(__name__)


class VisionPipeline:
    """Orchestrates ingestion from Kafka and delegates execution to Daft/Ray."""

    def __init__(
        self, config: VisionConfig, pipeline_graph: PipelineGraph
    ) -> None:
        self._config = config
        self._graph = pipeline_graph
        self._kafka_consumer: KafkaMultiTopicConsumer | None = None

        self._executor = ESKGPipelineExecutor(
            config=self._graph.config, vision_config=self._config
        )

    async def initialize(self) -> None:
        """Initialize Kafka connection."""
        topics = self._graph.get_kafka_topics()
        self._kafka_consumer = KafkaMultiTopicConsumer(
            self._config.kafka,
            topics=topics,
            schema_registry_url=self._config.kafka.schema_registry,
        )
        self._kafka_consumer.connect()

        logger.info("vision_pipeline_initialized")

    async def shutdown(self) -> None:
        """Release resources."""
        if self._kafka_consumer:
            self._kafka_consumer.close()
        logger.info("vision_pipeline_shutdown")

    async def process_batch(
        self, batch: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """Normalize a batch from Kafka and offload it to the distributed executor."""
        start = time.perf_counter()

        normalized_records = []
        for topic, payload in batch:
            if not isinstance(payload, dict):
                logger.warning("invalid_payload", type=type(payload))
                continue

            context = EventNormalizer.normalize(payload)
            normalized_records.append(context)

        if normalized_records:
            try:
                # Delegate all heavy lifting to Daft.
                await self._executor.execute_batch(normalized_records)
            except Exception as exc:
                logger.error("executor_batch_failed", error=str(exc))

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "batch_processed", size=len(batch), elapsed_ms=round(elapsed_ms, 2)
        )

    async def run(self) -> None:
        """Main loop consuming Kafka."""
        logger.info("vision_pipeline_started")
        if self._kafka_consumer is None:
            raise RuntimeError("Call await initialize() first.")

        try:
            for batch in self._kafka_consumer.stream():
                await self.process_batch(batch)
                self._kafka_consumer.commit()
        except KeyboardInterrupt:
            logger.info("pipeline_interrupted")
        finally:
            await self.shutdown()

    async def __aenter__(self) -> "VisionPipeline":
        await self.initialize()
        return self

    async def __aexit__(self, *args) -> None:
        await self.shutdown()
