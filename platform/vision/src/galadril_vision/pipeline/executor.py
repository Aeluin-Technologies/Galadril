"""Pure functional computation engine decoupled from scheduling structures."""

from __future__ import annotations

import time
from typing import Any, cast

import daft
import structlog
from galadril_pipeline.config import PipelineConfig, StepType
from galadril_pipeline.runtime.batch import PipelineResult

from galadril_vision.common.config import VisionConfig
from galadril_vision.compute.udfs import (
    DownloadDataWorker,
    resolve_entities_udf,
    run_inference_udf,
    sink_to_db_udf,
)
from galadril_vision.connectors.postgres.client import PostgresClient

logger = structlog.get_logger(__name__)


class ESKGPipelineExecutor:
    """Builds and materializes single linear execution graphs."""

    def __init__(
        self,
        config: PipelineConfig,
        vision_config: VisionConfig,
        pg_client: PostgresClient,
        vector_store: Any | None = None,
        graph_store: Any | None = None,
    ) -> None:
        """Instantiates execution dependencies and storage properties.

        Args:
            config: Pipeline configuration specifying processing steps.
            vision_config: Global core vision configuration parameters.
            pg_client: Connected PostgreSQL engine client wrapper.
            vector_store: Optional vector database interface for structural embeddings.
            graph_store: Optional property graph database interface for topology mappings.
        """
        self.config = config
        self.vision_config = vision_config
        self._pg_client = pg_client
        self._vector_store = vector_store
        self._graph_store = graph_store

    async def execute(self, parquet_uri: str) -> PipelineResult:
        """Assembles the complete lazy execution chain and triggers a single materialization step.

        Args:
            parquet_uri: Target remote data storage path containing incoming micro-batch payloads.

        Returns:
            A populated PipelineResult containing execution metrics.
        """
        start_time = time.perf_counter()

        df = daft.read_parquet(parquet_uri)

        download_worker = DownloadDataWorker(
            bucket=self.vision_config.raw_store.bucket,
            prefix=self.vision_config.raw_store.prefix,
            endpoint_url=self.vision_config.raw_store.endpoint_url,
            region_name=self.vision_config.raw_store.region_name,
            access_key=self.vision_config.raw_store.access_key,
            secret_key=self.vision_config.raw_store.secret_key,
        )

        download_expr = download_worker(
            df["storage_path"],
            df["record_id"],
            df["raw_payload"],
            df["metadata"],
        )
        df = df.with_column(
            "raw_data",
            cast(daft.Expression, download_expr),
        ).where(df["raw_data"].not_null())

        source_ids = {source.id for source in self.config.sources}

        for step in self.config.pipeline:
            extra_params = step.params.model_extra or {}

            upstream_node = step.input_from[0] if step.input_from else None
            if upstream_node in source_ids or not upstream_node:
                input_column = "raw_data"
            else:
                input_column = upstream_node

            match step.type:
                case StepType.INFERENCE:
                    model_name = step.model
                    action = extra_params.get("action")

                    if model_name is None or action is None:
                        raise ValueError(
                            f"Incomplete parameters for 'inference' step. "
                            f"Got model={model_name}, action={action}."
                        )

                    inference_expr = run_inference_udf(
                        df[input_column],
                        df["record_id"],
                        models_bucket=self.vision_config.models_store.bucket,
                        models_prefix=self.vision_config.models_store.prefix,
                        artifact_endpoint_url=self.vision_config.models_store.endpoint_url,
                        model_name=str(model_name),
                        action=str(action),
                    )
                    df = df.with_column(
                        step.step,
                        cast(daft.Expression, inference_expr),
                    )

                case StepType.RESOLVE:
                    modality = extra_params.get("modality")
                    threshold = extra_params.get("threshold")

                    if modality is None or threshold is None:
                        raise ValueError(
                            f"Incomplete parameters for 'resolve' step. "
                            f"Got modality={modality}, threshold={threshold}."
                        )

                    resolve_expr = resolve_entities_udf(
                        df[input_column],
                        df["tenant_id"],
                        postgres_config=self._pg_client._config,
                        modality=str(modality),
                        threshold=float(threshold),
                    )
                    df = df.with_column(
                        step.step, cast(daft.Expression, resolve_expr)
                    )

                case StepType.SINK:
                    entity_type = extra_params.get("entity_type")
                    modality = extra_params.get("modality")
                    edge_type = extra_params.get("edge_type")
                    state_type = extra_params.get("state_type")

                    if any(
                        p is None
                        for p in (entity_type, modality, edge_type, state_type)
                    ):
                        raise ValueError(
                            f"Incomplete parameters for 'sink' step. "
                            f"Got entity_type={entity_type}, modality={modality}, "
                            f"edge_type={edge_type}, state_type={state_type}."
                        )

                    sink_expr = sink_to_db_udf(
                        df[input_column],
                        df["record_id"],
                        df["source"],
                        df["tenant_id"],
                        df["event_type"],
                        df["raw_payload"],
                        postgres_config=self._pg_client._config,
                        entity_type=str(entity_type),
                        modality=str(modality),
                        edge_type=str(edge_type),
                        state_type=str(state_type),
                    )
                    df = df.with_column(
                        step.step, cast(daft.Expression, sink_expr)
                    )

                case _:
                    logger.warning(
                        "skipping_unknown_pipeline_type",
                        type=step.type,
                    )

        # Trigger graph execution after lazy transformations have been chained.
        materialized_df = df.collect()
        processed_count = len(materialized_df)
        duration = time.perf_counter() - start_time

        return PipelineResult(
            processed_records=processed_count,
            duration=duration,
        )
