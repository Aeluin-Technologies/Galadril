from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
import structlog
import daft
from uuid import uuid4
from datetime import datetime, timezone

from common.types import (
    EmbeddingModality,
    EntityEmbedding,
    EntityStateRecord,
    EventRecord,
    EventType,
    GraphVertex,
)
from pipeline.transforms import download_images_udf, run_inference_udf

if TYPE_CHECKING:
    from galadril_pipeline.config import PipelineConfig
    from connectors.postgres.vector import VectorStore
    from connectors.postgres.graph import GraphStore
    from common.config import VisionConfig

logger = structlog.get_logger(__name__)


class ESKGPipelineExecutor:
    """Executes the pipeline."""

    def __init__(
        self,
        config: PipelineConfig,
        vision_config: VisionConfig,
        vector_store: VectorStore,
        graph_store: GraphStore,
    ) -> None:
        self.config = config
        self.vision_config = vision_config
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._semaphore = asyncio.Semaphore(50)

    async def execute_batch(self, batch: list[dict[str, Any]]) -> None:
        """Process a batch through the DAG."""
        if not batch:
            return

        df = daft.from_pylist(batch)

        if "storage_path" in df.column_names:
            df = df.with_column(
                "image_data",
                download_images_udf(
                    df["storage_path"],
                    df["record_id"],
                    bucket=self.vision_config.image_store.bucket,
                    prefix=self.vision_config.image_store.prefix,
                    endpoint_url=self.vision_config.image_store.endpoint_url,
                ),
            )

        for step in self.config.pipeline:
            if step.type == "inference":
                model_name = step.model.split(".")[-1].lower()
                action = (
                    step.params.get("action", "embed")
                    if step.params
                    else "embed"
                )
                df = df.with_column(
                    f"{step.step}_result",
                    run_inference_udf(
                        df["image_data"],
                        df["record_id"],
                        artifact_bucket=self.vision_config.inference.bucket,
                        artifact_prefix=self.vision_config.inference.prefix,
                        artifact_endpoint_url=self.vision_config.inference.endpoint_url,
                        model_name=model_name,
                        action=action,
                    ),
                )

        computed_records = df.to_pylist()

        tasks = [
            self._process_eskg_resolution(record) for record in computed_records
        ]
        resolved_records = await asyncio.gather(*tasks)

        all_states: list[EntityStateRecord] = []
        all_embeddings: list[tuple[EntityEmbedding, str]] = []
        graph_tasks = []

        for record_results in resolved_records:
            all_states.extend(record_results.get("states", []))
            all_embeddings.extend(record_results.get("embeddings", []))

            for event in record_results.get("events", []):
                graph_tasks.append(
                    self._safe_graph_call(self._graph_store.insert_event, event)
                )
            for vertex in record_results.get("vertices", []):
                graph_tasks.append(
                    self._safe_graph_call(
                        self._graph_store.ensure_vertex, vertex
                    )
                )
            for edge_kwargs in record_results.get("edges", []):
                graph_tasks.append(
                    self._safe_graph_call(
                        self._graph_store.link_entity_to_event, **edge_kwargs
                    )
                )

        if graph_tasks:
            await asyncio.gather(*graph_tasks)

        if all_states:
            await self._graph_store.insert_entity_states_batch(all_states)
        if all_embeddings:
            await self._vector_store.store_embeddings_batch(all_embeddings)

    async def _safe_graph_call(self, func, *args, **kwargs):
        """Wrapper pour protéger les appels réseau du graphe par le sémaphore."""
        async with self._semaphore:
            return await func(*args, **kwargs)

    async def _process_eskg_resolution(
        self, record: dict[str, Any]
    ) -> dict[str, list[Any]]:
        """Resolve entities concurrently, then extract sink objects without inserting."""
        results = {
            "events": [],
            "vertices": [],
            "edges": [],
            "states": [],
            "embeddings": [],
        }

        for step in self.config.pipeline:
            if step.type == "resolve":
                input_col = f"{step.input_from[0]}_result"
                inference_data = record.get(input_col)
                if not inference_data or inference_data.get("error"):
                    continue

                modality = (
                    step.params.get("modality", "face")
                    if step.params
                    else "face"
                )
                threshold = (
                    step.params.get("threshold", 0.8) if step.params else 0.8
                )
                items = inference_data.get("prediction", {}).get("faces", [])

                for item in items:
                    vector = item.get("embedding")
                    if vector:
                        async with self._semaphore:
                            matches = await self._vector_store.find_similar(
                                embedding=vector,
                                modality=EmbeddingModality(modality),
                                top_k=1,
                            )
                        if matches and matches[0][1] >= threshold:
                            item["resolved_entity_id"] = matches[0][0]
                            item["is_unknown"] = False
                        else:
                            item["resolved_entity_id"] = (
                                f"unknown_{modality}_{uuid4().hex}"
                            )
                            item["is_unknown"] = True
                record[f"{step.step}_resolved"] = items

        for step in self.config.pipeline:
            if step.type == "sink":
                input_data = record.get(f"{step.input_from[0]}_resolved", [])

                event = EventRecord(
                    event_id=f"evt_{record['record_id']}",
                    event_type=EventType.OBSERVATION,
                    properties={"source": record.get("source", "unknown")},
                    timestamp=datetime.now(timezone.utc),
                )
                results["events"].append(event)

                for item in input_data:
                    entity_id = item.get("resolved_entity_id")
                    if not entity_id:
                        continue

                    entity_type = (
                        step.params.get("entity_type", "PERSON")
                        if step.params
                        else "PERSON"
                    )
                    modality = (
                        step.params.get("modality", "face")
                        if step.params
                        else "face"
                    )

                    results["vertices"].append(
                        GraphVertex(
                            vertex_id=entity_id,
                            label=entity_type,
                            properties={
                                "is_unknown": item.get("is_unknown", True)
                            },
                        )
                    )

                    results["edges"].append(
                        {
                            "entity_id": entity_id,
                            "event_id": event.event_id,
                            "role": "APPEARS_IN",
                        }
                    )

                    results["states"].append(
                        EntityStateRecord(
                            entity_id=entity_id,
                            event_id=event.event_id,
                            state_type="sighting",
                            state_value={
                                "confidence": item.get("confidence", 0.0),
                                "bbox": item.get("bbox"),
                            },
                            event_time=event.timestamp,
                        )
                    )

                    if item.get("embedding"):
                        emb_record = EntityEmbedding(
                            modality=EmbeddingModality(modality),
                            vector=item.get("embedding"),
                            metadata={"event_id": event.event_id},
                        )
                        results["embeddings"].append((emb_record, entity_id))

        return results
