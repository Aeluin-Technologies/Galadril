"""Pipeline orchestrator driven by galadril-pipeline DAG."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from connectors.kafka.consumer import KafkaMultiTopicConsumer
from connectors.postgres.client import PostgresClient
from connectors.postgres.graph import GraphStore
from connectors.postgres.vector import VectorStore

from galadril_inference import InferenceEngine, PredictionRequest
from galadril_inference.storage import S3Loader

from common.types import (
    EmbeddingModality,
    EntityEmbedding,
    EntityStateRecord,
    EventRecord,
    EventType,
    GraphEdge,
    GraphVertex,
)

if TYPE_CHECKING:
    from common.config import VisionConfig
    from galadril_pipeline.graph import PipelineGraph

logger = structlog.get_logger(__name__)


class VisionPipeline:
    """Orchestrates dynamic pipeline execution into the ESKG."""

    def __init__(
        self, config: VisionConfig, pipeline_graph: PipelineGraph
    ) -> None:
        self._config = config
        self._graph = pipeline_graph
        self._kafka_consumer: KafkaMultiTopicConsumer | None = None
        self._pg_client: PostgresClient | None = None
        self._vector_store: VectorStore | None = None
        self._graph_store: GraphStore | None = None
        self._engine: InferenceEngine | None = None

        self._topic_to_sources: dict[str, list[str]] = {}
        for source in self._graph.config.sources:
            self._topic_to_sources.setdefault(source.topic, []).append(
                source.id
            )

    async def initialize(self) -> None:
        """Initialize connections and load models based on execution plan."""
        topics = self._graph.get_kafka_topics()
        self._kafka_consumer = KafkaMultiTopicConsumer(
            self._config.kafka,
            topics=topics,
            schema_registry_url=self._config.kafka.schema_registry,
        )
        self._kafka_consumer.connect()

        self._pg_client = PostgresClient(self._config.postgres)
        await self._pg_client.connect()

        self._vector_store = VectorStore(self._pg_client, self._config.postgres)
        await self._vector_store.initialize()

        self._graph_store = GraphStore(self._pg_client, self._config.postgres)
        await self._graph_store.initialize()

        loader = S3Loader(
            bucket=self._config.inference.bucket,
            prefix=self._config.inference.prefix,
            endpoint_url=self._config.inference.endpoint_url,
        )
        self._engine = InferenceEngine(loader=loader)

        execution_plan = self._graph.get_execution_plan()
        for step in execution_plan:
            if step.type == "inference" and step.model:
                model_name = step.model.split(".")[-1].lower()
                if "face_recognition" in model_name:
                    model_name = "face_recognition"

                try:
                    self._engine.load_model(model_name)
                    logger.info("model_preloaded", model=model_name)
                except Exception as exc:
                    logger.error(
                        "model_load_failed", model=model_name, error=str(exc)
                    )

        logger.info("vision_pipeline_initialized")

    async def shutdown(self) -> None:
        """Release resources."""
        if self._kafka_consumer:
            self._kafka_consumer.close()
        if self._pg_client:
            await self._pg_client.close()
        logger.info("vision_pipeline_shutdown")

    def _get_outputs_for_step(self, step_id: str) -> list[str]:
        """Utility to find all steps that depend on `step_id`."""
        outputs: list[str] = []
        for step in self._graph.config.pipeline:
            if step.input_from and step_id in step.input_from:
                outputs.append(step.step)
        return outputs

    async def _execute_step(
        self, step_id: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a single step, enriching the context payload."""
        step = self._graph.get_step_by_id(step_id)

        if step.type == "inference":
            if not self._engine:
                raise RuntimeError("Inference engine not loaded")

            model_name = step.model.split(".")[-1].lower()
            if "face_recognition" in model_name:
                model_name = "face_recognition"

            payload = context.get("payload", {})
            action = (
                step.params.get("action", "embed") if step.params else "embed"
            )

            req = PredictionRequest(
                model_name=model_name,
                features={
                    "action": action,
                    "image": payload.get("image")
                    if isinstance(payload, dict)
                    else payload,
                    "text": payload.get("content")
                    if isinstance(payload, dict)
                    else None,
                },
            )
            result = await asyncio.to_thread(self._engine.predict, req)
            context[step_id] = result.prediction
            return context

        elif step.type == "resolve":
            input_step = step.input_from[0] if step.input_from else None
            inference_data = context.get(input_step, {})

            if not isinstance(inference_data, dict):
                inference_data = {}

            modality_str = (
                step.params.get("modality", "text") if step.params else "text"
            )
            threshold = float(
                step.params.get("threshold", 0.85) if step.params else 0.85
            )

            items = inference_data.get("entities")
            if not items:
                items = inference_data.get("faces", [])

            resolved_items = []

            for item in items:
                if not isinstance(item, dict):
                    continue

                vector = item.get("embedding")
                if vector and self._vector_store:
                    matches = await self._vector_store.find_similar(
                        embedding=vector,
                        modality=EmbeddingModality(modality_str),
                        top_k=1,
                    )

                    if matches and matches[0][1] >= threshold:
                        item["resolved_entity_id"] = matches[0][0]
                        item["is_unknown"] = False
                    else:
                        item["resolved_entity_id"] = (
                            f"entity_{modality_str}_{uuid4().hex}"
                        )
                        item["is_unknown"] = True
                else:
                    item["resolved_entity_id"] = f"entity_no_vec_{uuid4().hex}"
                    item["is_unknown"] = True

                resolved_items.append(item)

            context[step_id] = {
                "resolved_items": resolved_items,
                "relations": inference_data.get("relations", []),
            }
            return context

        elif step.type == "sink":
            input_step = step.input_from[0] if step.input_from else None
            sink_data = context.get(input_step, {})
            if not isinstance(sink_data, dict):
                sink_data = {}

            resolved_entities = sink_data.get("resolved_items", [])
            extracted_relations = sink_data.get("relations", [])
            metadata = context.get("payload", {})
            if not isinstance(metadata, dict):
                metadata = {}

            event_id = (
                metadata.get("record_id")
                or metadata.get("image_id")
                or uuid4().hex
            )
            raw_event_type = metadata.get("event_type", EventType.OBSERVATION)

            event = EventRecord(
                event_id=f"evt_{event_id}",
                event_type=EventType(raw_event_type)
                if isinstance(raw_event_type, str)
                else raw_event_type,
                properties={"source": metadata.get("source", "unknown")},
                timestamp=metadata.get("timestamp", datetime.now(timezone.utc)),
            )

            if self._graph_store:
                await self._graph_store.insert_event(event)

            id_mapping: dict[str, str] = {}

            for item in resolved_entities:
                if not isinstance(item, dict):
                    continue

                global_id = item.get("resolved_entity_id")
                internal_id = item.get("id") or item.get("face_id")

                if not global_id:
                    continue

                if internal_id:
                    id_mapping[internal_id] = global_id

                default_type = (
                    step.params.get("entity_type", "UNKNOWN")
                    if step.params
                    else "UNKNOWN"
                )
                entity_type = item.get("type", default_type)
                modality_str = (
                    step.params.get("modality", "text")
                    if step.params
                    else "text"
                )

                if self._graph_store:
                    await self._graph_store.ensure_vertex(
                        GraphVertex(
                            vertex_id=global_id,
                            label=entity_type,
                            properties={
                                "name": item.get("text", "unknown"),
                                "is_unknown": item.get("is_unknown", True),
                            },
                        )
                    )

                    await self._graph_store.link_entity_to_event(
                        entity_id=global_id,
                        event_id=event.event_id,
                        role="PARTICIPATED_IN",
                    )

                    state = EntityStateRecord(
                        entity_id=global_id,
                        event_id=event.event_id,
                        state_type="sighting",
                        state_value={
                            "confidence": item.get("confidence", 1.0),
                            "bbox": item.get("bbox"),
                        },
                        event_time=event.timestamp,
                    )
                    await self._graph_store.insert_entity_state(state)

                vector = item.get("embedding")
                if (
                    vector
                    and self._vector_store
                    and item.get("is_unknown", True)
                ):
                    emb_record = EntityEmbedding(
                        modality=EmbeddingModality(modality_str),
                        vector=vector,
                        metadata={
                            "event_id": event.event_id,
                            "text": item.get("text"),
                        },
                    )
                    await self._vector_store.store_embedding(
                        emb_record, global_id
                    )

            if self._graph_store and extracted_relations:
                for rel in extracted_relations:
                    if not isinstance(rel, dict):
                        continue

                    global_source = id_mapping.get(rel.get("source_id", ""))
                    global_target = id_mapping.get(rel.get("target_id", ""))
                    relation_type = rel.get(
                        "relation_type", "RELATED_TO"
                    ).upper()

                    if global_source and global_target:
                        await self._graph_store.create_edge(
                            GraphEdge(
                                source_vertex_id=global_source,
                                target_vertex_id=global_target,
                                edge_type=relation_type,
                                properties={"event_id": event.event_id},
                            )
                        )

            context[step_id] = {
                "status": "success",
                "nodes_inserted": len(resolved_entities),
                "edges_inserted": len(extracted_relations),
            }
            return context

        return context

    async def _route_message(self, source_id: str, payload: Any) -> None:
        """Route a single message through the DAG."""
        from connectors.kafka.schemas import EventNormalizer

        if not isinstance(payload, dict):
            logger.warning("invalid_payload", type=type(payload))
            return

        context = EventNormalizer.normalize(payload)

        next_steps = self._get_outputs_for_step(source_id)
        queue = [(step_id, context) for step_id in next_steps]

        while queue:
            step_id, current_context = queue.pop(0)
            try:
                updated_context = await self._execute_step(
                    step_id, current_context
                )

                further_steps = self._get_outputs_for_step(step_id)
                if not further_steps:
                    logger.debug("pipeline_sink_reached", step_id=step_id)
                else:
                    for n_step in further_steps:
                        queue.append((n_step, updated_context))
            except Exception as exc:
                logger.error(
                    "step_execution_failed", step_id=step_id, error=str(exc)
                )

    async def process_batch(
        self, batch: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """Process a batch from Kafka."""
        start = time.perf_counter()

        tasks = []
        for topic, payload in batch:
            sources = self._topic_to_sources.get(topic, [])
            for source_id in sources:
                tasks.append(self._route_message(source_id, payload))

        if tasks:
            await asyncio.gather(*tasks)

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
