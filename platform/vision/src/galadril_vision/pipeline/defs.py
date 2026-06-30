"""Dagster orchestration declarations for distributed Daft analytical analytical execution."""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any
import dagster as dg

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


# NOTE: Using sync boto3 here because Dagster sensors run on the Orchestration
# Daemon, which requires a blocking interface to return RunRequests. Async
# operations are delegated to the PipelineExecutor during actual job execution.
# See: https://dagster.io/blog/when-sync-isnt-enough
@dg.sensor(job=vision_pipeline_job, minimum_interval_seconds=30)
def s3_transit_fallback_sensor(context: dg.SensorEvaluationContext) -> Any:
    """Fallback directory scanning monitor to clear remaining transit keys upon API drop."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    last_processed_key = context.cursor or ""
    s3_bucket = os.getenv("VISION_STAGING_BUCKET", "galadril-staging")
    prefix = "staging/batches/"

    endpoint_url = os.getenv("AWS_S3_ENDPOINT_URL") or os.getenv(
        "AWS_ENDPOINT_URL_S3"
    )
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_REGION") or os.getenv(
        "AWS_DEFAULT_REGION", "us-east-1"
    )

    boto_config = Config(
        region_name=aws_region,
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "standard"},
        max_pool_connections=50,
    )

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        config=boto_config,
    )

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

    try:
        pages = paginator.paginate(**paginate_params)
        for page in pages:
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
                                            "config": {
                                                "batch_storage_path": s3_uri
                                            }
                                        }
                                    }
                                },
                            )
                        )
                        if allow_cursor_updates and key > new_cursor:
                            new_cursor = key
                    else:
                        allow_cursor_updates = False

    except (ClientError, BotoCoreError) as infrastructure_err:
        context.log.error(
            f"Sensor S3 API scanning failure on bucket '{s3_bucket}': {str(infrastructure_err)}"
        )
        return dg.SkipReason(
            f"Skipping evaluation due to remote S3 client infrastructure failure."
        )

    if new_cursor != last_processed_key:
        context.update_cursor(new_cursor)

    if run_requests:
        return run_requests

    return dg.SkipReason(
        "No unhandled staged records found in transit store out of grace period."
    )
