"""Linear Dagster pipeline topology configurations establishing asset dependency hierarchies."""

import os
import time
import uuid
from collections.abc import Generator
from typing import Any
from urllib.parse import urlparse

import daft
import dagster as dg
from galadril_pipeline.resources.kafka import KafkaResource
from galadril_pipeline.resources.postgres import PostgresResource
from galadril_pipeline.resources.s3 import S3ClientResource
from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult
from pydantic import PrivateAttr

from galadril_vision.causal.runner import AmarthCausalRunner
from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.postgres.graph import GraphStore
from galadril_vision.connectors.s3.transit import S3TransitService
from galadril_vision.pipeline.executor import ESKGPipelineExecutor
from galadril_vision.runtime import configure_runtime


class PipelineExecutorResource(
    dg.ConfigurableResource["PipelineExecutorResource"]
):
    """Configurable stateful factory translating platform configs into execution steps."""

    config_path: str
    ray_address: str | None = None
    db_provider: PostgresResource
    _executor: ESKGPipelineExecutor | None = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Builds the executor from the configuration visible to the Dagster worker."""
        vision_config = VisionConfig.from_yaml(self.config_path)
        if self.ray_address:
            daft.set_runner_ray(
                address=self.ray_address, noop_if_initialized=True
            )

        self._executor = ESKGPipelineExecutor(
            config=vision_config.to_pipeline_config(),
            vision_config=vision_config,
            pg_client=self.db_provider.client,
        )

    async def execute(self, uri: str) -> PipelineResult:
        """Executes the modern batch computation pipeline against the provided S3 URI."""
        if self._executor is None:
            raise RuntimeError(
                "PipelineExecutorResource accessed before setup."
            )
        return await self._executor.execute(uri)


class CausalRunnerResource(dg.ConfigurableResource["CausalRunnerResource"]):
    """Initializes causal execution from a deployment configuration path."""

    config_path: str
    db_provider: PostgresResource
    _runner: AmarthCausalRunner | None = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Builds the causal runner using the worker-local validated configuration."""
        vision_config = VisionConfig.from_yaml(self.config_path)
        pg_client = self.db_provider.client
        self._runner = AmarthCausalRunner(
            pg=pg_client,
            graph=GraphStore(
                config=vision_config.postgres,
                client=pg_client,
            ),
        )

    async def run(self, batch: BatchHandle[PipelineResult]) -> dict[str, Any]:
        """Executes causal analysis after validating resource initialization."""
        if self._runner is None:
            raise RuntimeError("CausalRunnerResource accessed before setup.")
        return await self._runner.run(batch=batch)


@dg.asset(
    compute_kind="kafka",
    description="Consumes a micro-batch from Kafka, staged to S3 as Parquet, and returns the URI.",
)
async def staged_batch(
    context: dg.AssetExecutionContext,
    kafka: KafkaResource,
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
    if not isinstance(tenant_id, str) or not all(
        c.isalnum() or c in "-_" for c in tenant_id
    ):
        tenant_id = "default"

    batch_id = str(uuid.uuid4())
    s3_key = f"batches/{tenant_id}/{batch_id}.parquet"

    transit_service = S3TransitService(s3_client_resource.client)
    s3_uri = await transit_service.upload_batch(
        key=s3_key, records=batch, format_type="parquet"
    )

    offsets = kafka.get_current_offsets()

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
    kafka: KafkaResource,
) -> BatchHandle[PipelineResult]:
    """Runs structural tracking assertions and safely commits Kafka offsets on success."""
    if execute_pipeline.payload.processed_records > 0:
        await causal_runner.run(batch=execute_pipeline)
        context.log.info("Causal model processing finished successfully.")

    if execute_pipeline.kafka_offsets:
        await kafka.commit_offsets(execute_pipeline.kafka_offsets)
        context.log.info(
            "Kafka consumer group offsets successfully committed to broker."
        )

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
    kafka: KafkaResource,
) -> Generator[dg.RunRequest | dg.SkipReason, None, None]:
    """Synchronous sensor evaluating streaming consumer group lag to generate deterministic keys."""
    if kafka.has_lag():
        offsets = kafka.get_current_offsets()
        offsets_str = "_".join(
            f"{t}_{p}_{o}"
            for t, partitions in sorted(offsets.items())
            for p, o in sorted(partitions.items())
        )
        run_key = (
            f"kafka_batch_{offsets_str}"
            if offsets_str
            else f"kafka_batch_{time.time()}"
        )
        yield dg.RunRequest(run_key=run_key)
    else:
        yield dg.SkipReason("No lag detected on configured Kafka topics.")


def parse_postgres_host_port(raw_host: str) -> tuple[str, int]:
    """Parses host and port securely, supporting IPv6 and standard formats."""
    if "://" not in raw_host:
        parsed = urlparse(f"tcp://{raw_host}")
    else:
        parsed = urlparse(raw_host)

    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    return host, port


def bootstrap_definitions() -> dg.Definitions:
    """Builds Dagster definitions from the validated deployment configuration."""
    config_path = os.getenv("PIPELINE_PATH", "bootstrap.yaml")
    base_cfg = VisionConfig.from_yaml(config_path)
    configure_runtime(base_cfg)

    parsed_host, parsed_port = parse_postgres_host_port(base_cfg.postgres.host)

    db_res = PostgresResource(
        host=parsed_host,
        port=parsed_port,
        username=base_cfg.postgres.user,
        password=base_cfg.postgres.password,
        database=base_cfg.postgres.database,
    )

    kafka_topics = base_cfg.get_kafka_topics()

    return dg.Definitions(
        assets=[staged_batch, execute_pipeline, run_causal],
        jobs=[vision_pipeline_job],
        sensors=[kafka_microbatch_sensor],
        resources={
            "s3_client_resource": S3ClientResource(
                bucket=base_cfg.connectors.s3.staging_bucket,
                endpoint_url=base_cfg.connectors.s3.endpoint,
                aws_access_key=base_cfg.connectors.s3.access_key,
                aws_secret_key=base_cfg.connectors.s3.secret_key,
                aws_region=base_cfg.connectors.s3.region,
            ),
            "kafka": KafkaResource(
                bootstrap_servers=base_cfg.kafka.bootstrap_servers,
                group_id=base_cfg.kafka.group_id,
                topics=kafka_topics,
            ),
            "pipeline_executor": PipelineExecutorResource(
                config_path=config_path,
                ray_address=base_cfg.ray.address,
                db_provider=db_res,
            ),
            "causal_runner": CausalRunnerResource(
                config_path=config_path,
                db_provider=db_res,
            ),
        },
    )


defs = bootstrap_definitions()
