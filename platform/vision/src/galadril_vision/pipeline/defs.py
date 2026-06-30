"""Dagster orchestration declarations for distributed Daft analytical batch execution."""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
import dagster as dg
from typing import Any

from galadril_vision.pipeline.executor import ESKGPipelineExecutor


class VisionPipelineConfig(dg.Config):
    """Execution runtime parameters injected into the distributed asset context."""

    batch_storage_path: str


@dg.asset(
    compute_kind="daft",
    op_tags={"cluster": "ray-inference-pool"},
    retry_policy=dg.RetryPolicy(
        max_retries=3, delay=15, backoff=dg.Backoff.EXPONENTIAL
    ),
)
async def vision_pipeline_batch(
    context: dg.AssetExecutionContext, config: VisionPipelineConfig
) -> None:
    """Invokes pure memory-efficient Daft engine computational transformations from staged pointer."""
    executor: ESKGPipelineExecutor = context.resources.pipeline_executor

    context.log.info(
        f"Beginning processing phase for data track target: {config.batch_storage_path}"
    )

    df_ingested = await executor.ingest_and_download(config.batch_storage_path)
    if df_ingested is not None:
        df_lazy = executor.transform_and_resolve(df_ingested)
        await executor.sink_and_causal(df_lazy)
        context.log.info(
            "Batch asset transformations completed and committed successfully."
        )
    else:
        context.log.warning(
            "Batch ingestion produced empty valid set. Computational graph skipped."
        )


vision_pipeline_job = dg.define_asset_job(
    name="vision_pipeline_job", selection="vision_pipeline_batch"
)


# NOTE: Using sync boto3 here because Dagster sensors run on the  Orchestration
# Daemon, which requires a blocking interface to return RunRequests. Async
# operations are delegated to the PipelineExecutor during actual job execution.
# See: https://dagster.io/blog/when-sync-isnt-enough
@dg.sensor(job=vision_pipeline_job, minimum_interval_seconds=30)
def s3_transit_fallback_sensor(context: dg.SensorEvaluationContext) -> Any:
    """Fallback directory scanning monitor to clear remaining transit keys upon API drop."""
    last_processed_key = context.cursor or ""
    s3_bucket = os.getenv("VISION_STAGING_BUCKET", "galadril-staging")
    prefix = "staging/batches/"

    import boto3

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")

    # Prevent scanning the entire bucket history on every evaluation loop.
    paginate_params = {"Bucket": s3_bucket, "Prefix": prefix}
    if last_processed_key:
        paginate_params["StartAfter"] = last_processed_key

    run_requests = []
    new_cursor = last_processed_key

    # 5-minute grace period to avoid race conditions.
    now = datetime.now(timezone.utc)
    grace_period = timedelta(minutes=5)
    allow_cursor_updates = True

    for page in paginator.paginate(**paginate_params):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".parquet"):
                last_modified = obj["LastModified"]

                if (now - last_modified) > grace_period:
                    batch_id = key.split("/")[-1].replace(".parquet", "")
                    s3_uri = f"s3://{s3_bucket}/{key}"

                    run_requests.append(
                        dg.RunRequest(
                            run_key=f"sensor_fallback_{batch_id}",
                            run_config={
                                "ops": {
                                    "vision_pipeline_batch": {
                                        "config": {"batch_storage_path": s3_uri}
                                    }
                                }
                            },
                        )
                    )
                    if allow_cursor_updates and key > new_cursor:
                        new_cursor = key
                else:
                    allow_cursor_updates = False

    if new_cursor != last_processed_key:
        context.update_cursor(new_cursor)

    if run_requests:
        return run_requests

    return dg.SkipReason(
        "No unhandled staged records found in transit store out of grace period."
    )
