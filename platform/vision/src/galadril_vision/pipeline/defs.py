"""Linear Dagster pipeline definitions utilizing sensor hooks."""

import asyncio
import concurrent.futures
import os
import time
import uuid
from typing import Any
import dagster as dg

from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult
from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.s3.client import S3Client
from galadril_vision.causal.runner import AmarthCausalRunner
from galadril_vision.pipeline.executor import ESKGPipelineExecutor


class StagedBatchConfig(dg.Config):
    """Configuration structure containing the target storage path for the remote batch."""

    batch_storage_path: str


@dg.asset(
    compute_kind="s3",
    description="Represents the remote Parquet storage pointer injected by the MinIO event stream.",
)
async def staged_batch(
    context: dg.AssetExecutionContext,
    config: StagedBatchConfig,
) -> Any:
    """Loads the staged remote storage pointer provided by the S3 configuration context.

    Args:
        context: The execution context provided by Dagster.
        config: The configuration object containing the remote file path.

    Returns:
        A BatchHandle wrapping the S3 location and execution metadata.
    """
    uri = config.batch_storage_path
    context.add_output_metadata({"staged_parquet_uri": uri})

    return BatchHandle[str](
        correlation_id=str(uuid.uuid4()),
        kafka_offsets={},
        started_at=time.time(),
        payload=uri,
    )


@dg.asset(
    compute_kind="daft",
    op_tags={"cluster": "ray-inference-pool"},
    description="Processes the data using Daft and Ray engines over the staged Parquet references.",
)
async def execute_pipeline(
    context: dg.AssetExecutionContext,
    staged_batch: Any,
    pipeline_executor: dg.ResourceParam[ESKGPipelineExecutor],
) -> Any:
    """Compiles and executes the memory-efficient processing pipeline over remote storage pointers.

    Args:
        context: The execution context provided by Dagster.
        staged_batch: The upstream asset containing the target batch storage path.
        pipeline_executor: The runtime executor responsible for executing Ray compute logic.

    Returns:
        A BatchHandle wrapping the processing execution metrics and duration logs.
    """
    uri = staged_batch.payload

    if not uri:
        result = PipelineResult(processed_records=0, duration=0.0)
    else:
        result = await pipeline_executor.execute(uri)

    context.add_output_metadata(
        {
            "processed_records": result.processed_records,
            "duration_seconds": result.duration,
        }
    )

    return BatchHandle[PipelineResult](
        correlation_id=staged_batch.correlation_id,
        kafka_offsets=staged_batch.kafka_offsets,
        started_at=staged_batch.started_at,
        payload=result,
    )


@dg.asset(
    compute_kind="causal",
    description="Executes downstream contextual tracking models using the computed execution batches.",
)
async def run_causal(
    context: dg.AssetExecutionContext,
    execute_pipeline: Any,
    causal_runner: dg.ResourceParam[AmarthCausalRunner],
) -> Any:
    """Applies contextual tracking models over internal state layers using explicit batch mappings.

    Args:
        context: The execution context provided by Dagster.
        execute_pipeline: The upstream processing results asset container.
        causal_runner: The computational engine interface executing the tracking models.

    Returns:
        The processed batch pipeline metrics execution container.
    """
    if execute_pipeline.payload.processed_records > 0:
        await causal_runner.run(batch=execute_pipeline)
        context.log.info(
            "Causal model processing steps finished execution successfully."
        )

    return execute_pipeline


vision_pipeline_job = dg.define_asset_job(
    name="vision_pipeline_job",
    selection=dg.AssetSelection.assets(
        staged_batch, execute_pipeline, run_causal
    ),
)


# TODO: use pulling from S3 notification instead of polling.
@dg.sensor(
    job=vision_pipeline_job,
    minimum_interval_seconds=2,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def minio_parquet_sensor(context: dg.SensorEvaluationContext):
    """Polls MinIO using a synchronous wrapper around the asynchronous S3Client engine.

    Args:
        context: The evaluation context provided by the Dagster daemon loop.

    Yields:
        RunRequest objects targeting new partitions, or a SkipReason if nothing is found.
    """
    config_path = os.getenv("PIPELINE_PATH", "bootstrap.yaml")
    try:
        base_cfg = VisionConfig.from_yaml(config_path)
    except Exception as exc:
        yield dg.SkipReason(
            f"Failed to load infrastructure bootstrap configuration: {str(exc)}"
        )
        return

    bucket_name = base_cfg.connectors.s3.staging_bucket
    last_processed_key = context.cursor or ""
    prefix = "batches/"

    s3_client = S3Client(
        bucket=bucket_name,
        endpoint_url=base_cfg.connectors.s3.endpoint,
        aws_access_key=base_cfg.connectors.s3.access_key,
        aws_secret_key=base_cfg.connectors.s3.secret_key,
        aws_region=base_cfg.connectors.s3.region,
    )

    async def _fetch_keys() -> list[str]:
        async with s3_client as client:
            return await client.list_object_keys(
                prefix=prefix, suffix=".parquet"
            )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, _fetch_keys())
            all_keys = future.result()
    except Exception as exc:
        yield dg.SkipReason(
            f"Asynchronous MinIO catalog lookup failed: {str(exc)}"
        )
        return

    new_files = sorted([key for key in all_keys if key > last_processed_key])

    if not new_files:
        yield dg.SkipReason(
            "No new staged Parquet batches detected under the target partition prefix."
        )
        return

    for file_key in new_files:
        s3_uri = f"s3://{bucket_name}/{file_key}"
        yield dg.RunRequest(
            run_key=file_key,
            run_config={
                "ops": {
                    "staged_batch": {"config": {"batch_storage_path": s3_uri}}
                }
            },
        )

    context.update_cursor(new_files[-1])
