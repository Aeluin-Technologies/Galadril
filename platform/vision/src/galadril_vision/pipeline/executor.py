"""Pure functional computation engine decoupled from scheduling structures."""

from __future__ import annotations

import time
from typing import Any, Optional, cast
import daft

from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.runtime.batch import PipelineResult
from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.compute.udfs import (
    DownloadDataWorker,
    run_inference_udf,
    resolve_entities_udf,
    sink_to_db_udf,
)


class ESKGPipelineExecutor:
    """Builds and materializes single linear execution graphs without holding internal orchestration knowledge."""

    def __init__(
        self,
        config: PipelineConfig,
        vision_config: VisionConfig,
        pg_client: PostgresClient,
        vector_store: Optional[Any] = None,
        graph_store: Optional[Any] = None,
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

        # Initialize Daft Lazy DataFrame pointing directly to S3 Parquet
        df = daft.read_parquet(parquet_uri)

        download_worker = DownloadDataWorker(
            bucket=self.vision_config.raw_store.bucket,
            prefix=self.vision_config.raw_store.prefix,
            endpoint_url=self.vision_config.raw_store.endpoint_url,
            region_name=self.vision_config.raw_store.region_name,
            access_key=self.vision_config.raw_store.access_key,
            secret_key=self.vision_config.raw_store.secret_key,
        )

        # Cast the worker call to a Daft Expression to fulfill strict typing guards
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

        # Build dynamic execution plan without eagerly loading records into memory
        for step in self.config.pipeline:
            # Safe dynamic attribute resolution to bypass Pylance structural inference blocks
            step_type = getattr(step, "step_type", None)
            params = getattr(step, "params", None)

            if step_type == "inference":
                model_name = str(
                    getattr(params, "model", "face_default")
                    if params
                    else "face_default"
                )
                action = str(
                    getattr(params, "action", "embed") if params else "embed"
                )

                inference_expr = run_inference_udf(
                    df["raw_data"],
                    df["record_id"],
                    models_bucket=self.vision_config.models_store.bucket,
                    models_prefix=self.vision_config.models_store.prefix,
                    artifact_endpoint_url=self.vision_config.models_store.endpoint_url,
                    model_name=model_name,
                    action=action,
                )
                df = df.with_column(
                    "inference_result", cast(daft.Expression, inference_expr)
                )

            elif step_type == "resolve":
                modality = str(
                    getattr(params, "modality", "face") if params else "face"
                )
                threshold = float(
                    getattr(params, "threshold", 0.75) if params else 0.75
                )

                resolve_expr = resolve_entities_udf(
                    df["inference_result"],
                    df["tenant_id"],
                    postgres_config=self._pg_client._config,
                    modality=modality,
                    threshold=threshold,
                )
                df = df.with_column(
                    "resolved_entities", cast(daft.Expression, resolve_expr)
                )

            elif step_type == "sink":
                entity_type = str(
                    getattr(params, "entity_type", "Person")
                    if params
                    else "Person"
                )
                modality = str(
                    getattr(params, "modality", "face") if params else "face"
                )
                edge_type = str(
                    getattr(params, "edge_type", "IDENTIFIED_AS")
                    if params
                    else "IDENTIFIED_AS"
                )
                state_type = str(
                    getattr(params, "state_type", "observation")
                    if params
                    else "observation"
                )

                sink_expr = sink_to_db_udf(
                    df["resolved_entities"],
                    df["record_id"],
                    df["source"],
                    df["tenant_id"],
                    df["event_type"],
                    df["raw_payload"],
                    postgres_config=self._pg_client._config,
                    entity_type=entity_type,
                    modality=modality,
                    edge_type=edge_type,
                    state_type=state_type,
                )
                df = df.with_column(
                    "sink_status", cast(daft.Expression, sink_expr)
                )

        # Trigger Ray cluster materialization and resolve row count using Python sizing protocols
        materialized_df = df.collect()
        processed_count = len(materialized_df)
        duration = time.perf_counter() - start_time

        return PipelineResult(
            processed_records=processed_count,
            duration=duration,
        )
