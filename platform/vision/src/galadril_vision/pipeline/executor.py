"""galadril-vision pipeline executor."""

from __future__ import annotations

import time
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
from galadril_vision.telemetry.tracing import instrument

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

    @property
    def batch_timeout_s(self) -> float | None:
        """Return the configured maximum duration for a pipeline batch."""
        return self.vision_config.batch_timeout_s

    def _apply_inference_step(
        self,
        df: Any,
        *,
        step_name: str,
        model_name: str,
        action: str,
    ) -> Any:
        """Attach an inference result column for one pipeline step."""
        logger.debug(
            "appending_lazy_inference_step",
            step=step_name,
            model=model_name,
            action=action,
        )
        return df.with_column(
            f"{step_name}_result",
            run_inference_udf(
                df["raw_data"],
                df["record_id"],
                models_bucket=self.vision_config.models_store.bucket,
                models_prefix=self.vision_config.models_store.prefix,
                artifact_endpoint_url=self.vision_config.models_store.endpoint_url,
                model_name=model_name,
                action=action,
            ),
        )

    def _apply_resolve_step(
        self,
        df: Any,
        *,
        step_name: str,
        input_col: str,
        postgres_config: Any,
        modality: str,
        threshold: Any,
    ) -> Any:
        """Attach an entity-resolution result column for one pipeline step."""
        logger.debug(
            "appending_lazy_resolve_step",
            step=step_name,
            target_column=input_col,
            modality=modality,
        )
        return df.with_column(
            f"{step_name}_resolved",
            resolve_entities_udf(
                df[input_col],
                df["tenant_id"],
                postgres_config=postgres_config,
                modality=modality,
                threshold=threshold,
            ),
        )

    def _apply_sink_step(
        self,
        df: Any,
        *,
        step_name: str,
        input_col: str,
        postgres_config: Any,
        entity_type: str,
        modality: str,
        edge_type: str,
        state_type: str,
    ) -> Any:
        """Attach a sink status column for one pipeline step."""
        logger.debug(
            "appending_lazy_sink_step",
            step=step_name,
            target_column=input_col,
            entity_type=entity_type,
        )
        return df.with_column(
            f"{step_name}_status",
            sink_to_db_udf(
                df[input_col],
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

    @instrument("execute_pipeline_batch")
    async def execute_batch(self, batch: list[dict[str, Any]]) -> None:
        """Process a batch through the distributed cluster DAG."""
        raw_batch_size = len(batch)
        if not batch:
            logger.debug("pipeline_execute_batch_skipped_empty_input")
            return

        logger.info("pipeline_batch_received", raw_records_count=raw_batch_size)

        canonical: list[dict[str, Any]] = []
        validation_failures = 0

        for item in batch:
            try:
                rec = CanonicalRecord.model_validate(item)
                canonical.append(rec.model_dump(mode="python"))
            except ValidationError as exc:
                validation_failures += 1
                logger.warning(
                    "batch_record_rejected",
                    record_id=item.get("record_id", "unknown"),
                    errors=exc.errors(),
                )

        if not canonical:
            logger.error(
                "batch_rejected_all_records",
                raw_records_count=raw_batch_size,
                validation_failures=validation_failures,
            )
            return

        logger.debug(
            "batch_schema_validation_summary",
            accepted=len(canonical),
            rejected=validation_failures,
        )

        df = daft.from_pylist(canonical)

        if "storage_path" in df.column_names:
            logger.debug("building_lazy_data_download_expression_graph")
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

            logger.info("materializing_download_and_filter_stage_start")
            start_collect = time.perf_counter()
            df = df.collect()
            collect_duration = time.perf_counter() - start_collect
            logger.info(
                "materializing_download_and_filter_stage_complete",
                active_records=len(df),
                duration_s=round(collect_duration, 4),
            )

        if len(df) == 0:
            logger.warning(
                "batch_emptied_after_raw_data_load",
                original_size=len(canonical),
                failures=validation_failures,
            )
            return

        postgres_config = self._pg_client._config
        step_models: dict[str, str] = {}
        step_modalities: dict[str, str] = {}
        pipeline_steps_count = len(self.config.pipeline)

        logger.info(
            "assembling_distributed_dag_expressions",
            steps_count=pipeline_steps_count,
        )

        for step in self.config.pipeline:
            logger.info(
                "compiling_pipeline_step", step=step.step, type=step.type
            )

            if step.type == "inference":
                model_name = _normalize_model_name(step.model)
                step_models[step.step] = model_name
                action = (
                    _get_step_param(step.params, "action", "embed") or "embed"
                )
                df = self._apply_inference_step(
                    df,
                    step_name=step.step,
                    model_name=model_name,
                    action=action,
                )

            elif step.type == "resolve":
                if not step.input_from:
                    logger.error(
                        "missing_upstream_input_declaration", step=step.step
                    )
                    continue
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

                df = self._apply_resolve_step(
                    df,
                    step_name=step.step,
                    input_col=input_col,
                    postgres_config=postgres_config,
                    modality=modality,
                    threshold=threshold,
                )

            elif step.type == "sink":
                if not step.input_from:
                    logger.error(
                        "missing_upstream_input_declaration", step=step.step
                    )
                    continue
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

                df = self._apply_sink_step(
                    df,
                    step_name=step.step,
                    input_col=f"{step.input_from[0]}_resolved",
                    postgres_config=postgres_config,
                    entity_type=entity_type,
                    modality=modality,
                    edge_type=edge_type,
                    state_type=state_type,
                )

        logger.info(
            "executing_distributed_computations_graph_start",
            records_count=len(df),
        )
        start_pipeline_run = time.perf_counter()

        try:
            df.collect()
            pipeline_duration = time.perf_counter() - start_pipeline_run
            logger.info(
                "executing_distributed_computations_graph_complete",
                records_processed=len(df),
                duration_s=round(pipeline_duration, 4),
            )
        except Exception as exc:
            logger.exception("distributed_computations_graph_execution_failed")
            raise exc

        await self._run_causal_triggers(completed_step="sink")

    async def _run_causal_triggers(self, completed_step: str) -> None:
        """Evaluate and run causal trigger dependencies based on completion state flags."""
        for step in self.config.pipeline:
            if step.type != "causal" or not step.params:
                continue
            if getattr(step.params, "trigger", None) != "on_step_completed":
                continue
            if getattr(step.params, "on_step", None) != completed_step:
                continue

            try:
                spec = build_slice_spec_from_step_params(step.params)
                target_outcome = (
                    getattr(step.params, "amarth_target_outcome", None)
                    or "state_avg_confidence.observation"
                )
                window_size = getattr(step.params, "amarth_window_size", None)

                logger.info(
                    "causal_job_started",
                    step=step.step,
                    trigger=step.params.trigger,
                    on_step=step.params.on_step,
                    target=spec.target,
                    lookback=str(getattr(step.params, "lookback", "default")),
                    bucket=getattr(step.params, "bucket", None),
                    max_events=spec.max_events,
                    max_states=spec.max_states,
                    target_outcome=target_outcome,
                )

                start_causal = time.perf_counter()
                result = await self._causal.run(
                    spec=spec,
                    target_outcome=target_outcome,
                    window_size=window_size,
                )
                causal_duration = time.perf_counter() - start_causal

                logger.info(
                    "causal_job_completed",
                    step=step.step,
                    status=result.get("status", "unknown"),
                    persisted_edges=result.get("persisted_edges"),
                    cache_key=result.get("cache_key"),
                    effects_count=len(result.get("effects", {}))
                    if result.get("effects")
                    else 0,
                    duration_s=round(causal_duration, 4),
                )
            except Exception as exc:
                logger.exception(
                    "causal_inference_engine_step_failed", step=step.step
                )
