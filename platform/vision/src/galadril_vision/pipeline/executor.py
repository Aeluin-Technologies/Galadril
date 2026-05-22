"""galadril-vision pipeline executor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import structlog
import daft
from uuid import uuid4
from datetime import datetime, timezone
from pydantic import ValidationError

from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.common.types import (
    EmbeddingModality,
    EntityEmbedding,
    EntityStateRecord,
    EventRecord,
    EventType,
    GraphVertex,
)
from galadril_vision.pipeline.transforms import (
    download_images_udf,
    run_inference_udf,
)

from galadril_vision.causal.runner import (
    AmarthCausalRunner,
    build_slice_spec_from_step_params,
)

if TYPE_CHECKING:
    from galadril_pipeline.config import PipelineConfig  # type: ignore
    from galadril_vision.connectors.postgres.vector import VectorStore
    from galadril_vision.connectors.postgres.graph import GraphStore
    from galadril_vision.connectors.postgres.client import PostgresClient
    from galadril_vision.common.config import VisionConfig

logger = structlog.get_logger(__name__)


class ESKGPipelineExecutor:
    """Executes the pipeline."""

    def __init__(
        self,
        config: PipelineConfig,
        vision_config: VisionConfig,
        vector_store: VectorStore,
        graph_store: GraphStore,
        pg_client: PostgresClient,
    ) -> None:
        self.config = config
        self.vision_config = vision_config
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._pg_client = pg_client
        self._causal = AmarthCausalRunner(pg_client, graph_store)

    async def execute_batch(self, batch: list[dict[str, Any]]) -> None:
        """Process a batch through the DAG."""
        if not batch:
            return

        canonical: list[dict[str, Any]] = []
        for item in batch:
            try:
                rec = CanonicalRecord.model_validate(item)
                canonical.append(rec.model_dump(mode="python"))
            except ValidationError as exc:
                logger.warning("batch_record_rejected", error=str(exc))

        if not canonical:
            logger.warning("batch_rejected_all_records")
            return

        df = daft.from_pylist(canonical)

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

        for record in computed_records:
            await self._process_eskg_logic(record)

        await self._run_causal_triggers(completed_step="sink")

    async def _run_causal_triggers(self, completed_step: str) -> None:
        for step in self.config.pipeline:
            if step.type != "causal" or not step.params:
                continue
            if step.params.trigger != "on_step_completed":
                continue
            if step.params.on_step != completed_step:
                continue

            spec = build_slice_spec_from_step_params(step.params)

            target_outcome = (
                step.params.amarth_target_outcome
                or "state_avg_confidence.sighting"
            )
            window_size = step.params.amarth_window_size

            logger.info(
                "causal_job_started",
                step=step.step,
                trigger=step.params.trigger,
                on_step=step.params.on_step,
                target=spec.target,
                lookback=str(step.params.lookback),
                bucket=getattr(step.params, "bucket", None),
                max_events=spec.max_events,
                max_states=spec.max_states,
                target_outcome=target_outcome,
            )

            result = await self._causal.run(
                spec=spec,
                target_outcome=target_outcome,
                window_size=window_size,
            )

            logger.info(
                "causal_job_completed",
                step=step.step,
                status=result.get("status", "unknown"),
                persisted_edges=result.get("persisted_edges"),
                cache_key=result.get("cache_key"),
                effects=result.get("effects"),
            )

    async def _process_eskg_logic(self, record: dict[str, Any]) -> None:
        """Execute resolving and sinking sequentially per record."""
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

            elif step.type == "sink":
                input_data = record.get(f"{step.input_from[0]}_resolved", [])

                event = EventRecord(
                    event_id=f"evt_{record['record_id']}",
                    event_type=EventType.from_str(record.get("event_type")),
                    properties={"source": record.get("source", "unknown")},
                    timestamp=datetime.now(timezone.utc),
                )
                await self._graph_store.insert_event(event)

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

                    await self._graph_store.ensure_vertex(
                        GraphVertex(
                            vertex_id=entity_id,
                            label=entity_type,
                            properties={
                                "is_unknown": item.get("is_unknown", True)
                            },
                        )
                    )
                    await self._graph_store.link_entity_to_event(
                        entity_id=entity_id,
                        event_id=event.event_id,
                        role="APPEARS_IN",
                    )

                    state = EntityStateRecord(
                        entity_id=entity_id,
                        event_id=event.event_id,
                        state_type="sighting",
                        state_value={
                            "confidence": item.get("confidence", 0.0),
                            "bbox": item.get("bbox"),
                        },
                        event_time=event.timestamp,
                    )
                    await self._graph_store.insert_entity_state(state)

                    if item.get("embedding"):
                        emb_record = EntityEmbedding(
                            modality=EmbeddingModality(modality),
                            vector=item.get("embedding"),
                            metadata={"event_id": event.event_id},
                        )
                        await self._vector_store.store_embedding(
                            emb_record, entity_id
                        )
