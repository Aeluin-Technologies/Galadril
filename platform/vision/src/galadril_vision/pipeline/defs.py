"""Linear Dagster pipeline topology configurations establishing asset dependency hierarchies."""

import os
import time
import uuid
from typing import Optional

import daft
import dagster as dg
from pydantic import PrivateAttr

from galadril_pipeline.resources.causal import CausalRunnerResource
from galadril_pipeline.resources.config import VisionConfigResource
from galadril_pipeline.resources.kafka import VisionKafkaResource
from galadril_pipeline.resources.postgres import PostgresResource
from galadril_pipeline.resources.s3 import S3ClientResource
from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult

from galadril_vision.connectors.s3.transit import S3TransitService
from galadril_vision.pipeline.executor import ESKGPipelineExecutor


class PipelineExecutorResource(dg.ConfigurableResource):
    """Configurable stateful factory translating platform configs into execution steps."""

    config_provider: dg.ResourceDependency[VisionConfigResource]
    db_provider: dg.ResourceDependency[PostgresResource]
    _executor: Optional[ESKGPipelineExecutor] = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Sets up the pipeline executor and configures backend environment variables."""
        base_cfg = self.config_provider.vision_config

        os.environ["AWS_ACCESS_KEY_ID"] = base_cfg.connectors.s3.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = base_cfg.connectors.s3.secret_key
        os.environ["AWS_DEFAULT_REGION"] = base_cfg.connectors.s3.region
        os.environ["VISION_STAGING_BUCKET"] = (
            base_cfg.connectors.s3.staging_bucket
        )

        if base_cfg.ray.address:
            daft.set_runner_ray(
                address=base_cfg.ray.address, noop_if_initialized=True
            )

        self._executor = ESKGPipelineExecutor(
            config=self.config_provider.pipeline_config,
            vision_config=base_cfg,
            pg_client=self.db_provider.client,
        )

    async def execute(self, uri: str) -> PipelineResult:
        """Executes the modern batch computation pipeline against the provided S3 URI."""
        if self._executor is None:
            raise RuntimeError(
                "PipelineExecutorResource accessed before setup."
            )
        return await self._executor.execute(uri)


@dg.asset(
    compute_kind="kafka",
    description="Consumes a micro-batch from Kafka, stages to S3 as Parquet, and returns the URI.",
)
async def staged_batch(
    context: dg.AssetExecutionContext,
    kafka: VisionKafkaResource,
    s3_client_resource: S3ClientResource,
) -> BatchHandle[str]:
    """Consumes records from Kafka streams and uploads them cleanly to persistent objects."""
    batch = await kafka.poll_batch(max_records=1000, timeout_s=5.0)

    if not batch:
        context.log.info("No records fetched from Kafka.")
        return BatchHandle[str](
            correlation_id=str(uuid.uuid4()),
            kafka_offsets={},
            started_at=time.time(),
            payload="",
        )

    tenant_id = batch[0].get("tenant_id", "default")
    batch_id = str(uuid.uuid4())
    s3_key = f"batches/{tenant_id}/{batch_id}.parquet"

    transit_service = S3TransitService(s3_client_resource.client)
    s3_uri = await transit_service.upload_batch(
        key=s3_key, records=batch, format_type="parquet"
    )

    offsets = kafka.get_current_offsets()
    await kafka.commit_offsets(offsets)

    context.add_output_metadata(
        {"staged_parquet_uri": s3_uri, "records_ingested": len(batch)}
    )

    return BatchHandle[str](
        correlation_id=batch_id,
        kafka_offsets=offsets,
        started_at=time.time(),
        payload=s3_uri,
    )


@dg.asset(
    compute_kind="daft",
    op_tags={"cluster": "ray-inference-pool"},
    description="Processes the data using Daft and Ray engines over the staged Parquet references.",
)
async def execute_pipeline(
    context: dg.AssetExecutionContext,
    staged_batch: BatchHandle[str],
    pipeline_executor: PipelineExecutorResource,
) -> BatchHandle[PipelineResult]:
    """Executes distributed parsing pipelines via parallelized cluster compute environments."""
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
    execute_pipeline: BatchHandle[PipelineResult],
    causal_runner: CausalRunnerResource,
) -> BatchHandle[PipelineResult]:
    """Runs structural tracking and lineage assertions if target records were populated."""
    if execute_pipeline.payload.processed_records > 0:
        await causal_runner.run(batch=execute_pipeline)
        context.log.info("Causal model processing finished successfully.")
    return execute_pipeline


vision_pipeline_job = dg.define_asset_job(
    name="vision_pipeline_job",
    selection=dg.AssetSelection.assets(
        staged_batch, execute_pipeline, run_causal
    ),
)


@dg.sensor(
    job=vision_pipeline_job,
    minimum_interval_seconds=15,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def kafka_microbatch_sensor(
    context: dg.SensorEvaluationContext,
    kafka: VisionKafkaResource,
):
    """Synchronous sensor evaluating streaming consumer group lag to trigger asset targets."""
    if kafka.has_lag():
        yield dg.RunRequest(run_key=f"kafka_batch_{time.time()}")
    else:
        yield dg.SkipReason("No lag detected on configured Kafka topics.")


config_res = VisionConfigResource()
db_res = PostgresResource(config_provider=config_res)
s3_res = S3ClientResource(config_provider=config_res)
kafka_res = VisionKafkaResource(
    config_provider=config_res, bootstrap_servers="", group_id="", topics=[]
)
pipeline_res = PipelineExecutorResource(
    config_provider=config_res, db_provider=db_res
)
causal_res = CausalRunnerResource(
    config_provider=config_res, db_provider=db_res
)

defs = dg.Definitions(
    assets=[staged_batch, execute_pipeline, run_causal],
    jobs=[vision_pipeline_job],
    sensors=[kafka_microbatch_sensor],
    resources={
        "config_provider": config_res,
        "db_provider": db_res,
        "s3_client_resource": s3_res,
        "pipeline_executor": pipeline_res,
        "causal_runner": causal_res,
        "kafka": kafka_res,
    },
)
