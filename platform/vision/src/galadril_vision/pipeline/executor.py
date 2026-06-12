"""galadril-vision pipeline executor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import structlog
import daft
from pydantic import ValidationError

from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.pipeline.transforms import (
    download_data_udf,
    run_inference_udf,
    resolve_entities_udf,
    sink_to_db_udf,
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
_MODEL_ARTIFACT_EXTENSIONS = frozenset(
    ("bin", "joblib", "model", "onnx", "pkl", "pt", "pth", "safetensors")
)


def _get_step_param(params: Any, name: str, default: Any = None) -> Any:
    """Retrieves a step parameter from a Pydantic object or plain dictionary."""
    if params is None:
        return default
    if isinstance(params, dict):
        return params.get(name, default)
    return getattr(params, name, default)


def _normalize_model_name(model: str | None) -> str:
    """Converts configured model references into vector-store partition keys."""
    model_name = (model or "default.model").strip().lower()
    name = model_name.rsplit("/", 1)[-1]
    parts = name.rsplit(".", 1)
    if len(parts) == 2 and parts[1] in _MODEL_ARTIFACT_EXTENSIONS:
        return parts[0]
    return parts[-1]


class ESKGPipelineExecutor:
    """Executes the pipeline completely distributed via Daft expressions."""

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
        """Process a batch through the distributed cluster DAG."""
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
                "raw_data",
                download_data_udf(
                    df["storage_path"],
                    df["record_id"],
                    df["raw_payload"],
                    df["metadata"],
                    bucket=self.vision_config.raw_store.bucket,
                    prefix=self.vision_config.raw_store.prefix,
                    endpoint_url=self.vision_config.raw_store.endpoint_url,
                    region_name=self.vision_config.raw_store.region_name,
                    access_key=self.vision_config.raw_store.access_key,
                    secret_key=self.vision_config.raw_store.secret_key,
                ),
            )

            df = df.where(df["raw_data"].not_null())
            df = df.collect()

        if len(df) == 0:
            logger.warning("batch_emptied_after_raw_data_load")
            return

        postgres_config = self._pg_client._config
        step_models: dict[str, str] = {}
        step_modalities: dict[str, str] = {}

        for step in self.config.pipeline:
            if step.type == "inference":
                model_name = _normalize_model_name(step.model)
                step_models[step.step] = model_name
                action = (
                    _get_step_param(step.params, "action", "embed") or "embed"
                )

                df = df.with_column(
                    f"{step.step}_result",
                    run_inference_udf(
                        df["raw_data"],
                        df["record_id"],
                        artifact_bucket=self.vision_config.inference.bucket,
                        artifact_prefix=self.vision_config.inference.prefix,
                        artifact_endpoint_url=self.vision_config.inference.endpoint_url,
                        model_name=model_name,
                        action=action,
                    ),
                )

            elif step.type == "resolve":
                input_col = f"{step.input_from[0]}_result"
                upstream_model = step_models.get(step.input_from[0], "face")
                modality = (
                    _get_step_param(step.params, "modality")
                    or _get_step_param(step.params, "model")
                    or upstream_model
                )
                modality = _normalize_model_name(str(modality))
                step_modalities[step.step] = modality
                threshold = _get_step_param(step.params, "threshold", 0.7)
                if threshold is None:
                    threshold = 0.7

                df = df.with_column(
                    f"{step.step}_resolved",
                    resolve_entities_udf(
                        df[input_col],
                        df["tenant_id"],
                        postgres_config=postgres_config,
                        modality=modality,
                        threshold=threshold,
                    ),
                )

            elif step.type == "sink":
                entity_type = "PERSON"
                upstream_step = step.input_from[0]
                upstream_model = step_modalities.get(
                    upstream_step,
                    step_models.get(upstream_step, "face"),
                )
                entity_type = (
                    _get_step_param(step.params, "entity_type", "Entity")
                    or "Entity"
                )
                modality = (
                    _get_step_param(step.params, "modality")
                    or _get_step_param(step.params, "model")
                    or upstream_model
                )
                modality = _normalize_model_name(str(modality))
                edge_type = (
                    _get_step_param(step.params, "edge_type", "DERIVED_FROM")
                    or "DERIVED_FROM"
                )
                state_type = (
                    _get_step_param(step.params, "state_type", "observation")
                    or "observation"
                )

                df = df.with_column(
                    f"{step.step}_status",
                    sink_to_db_udf(
                        df[f"{step.input_from[0]}_resolved"],
                        df["record_id"],
                        df["source"],
                        df["tenant_id"],
                        df["event_type"],
                        df["raw_payload"],
                        postgres_config=postgres_config,
                        entity_type=entity_type,
                        modality=modality,
                        edge_type=edge_type,
                        state_type=state_type,
                    ),
                )

        df.collect()

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
                or "state_avg_confidence.observation"
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
