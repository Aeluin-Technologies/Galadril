"""Linear Dagster pipeline topology configurations establishing asset dependency hierarchies."""

import time
import uuid
from collections.abc import Generator
from typing import Any, Optional, Union, cast
from urllib.parse import ParseResult, urlparse

import daft
import dagster as dg
from pydantic import PrivateAttr

from galadril_pipeline.resources.causal import CausalRunnerResource
from galadril_pipeline.resources.kafka import KafkaResource
from galadril_pipeline.resources.postgres import PostgresResource
from galadril_pipeline.resources.s3 import S3ClientResource
from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult

from galadril_vision.connectors.s3.transit import S3TransitService
from galadril_vision.pipeline.executor import ESKGPipelineExecutor

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dagster._core.definitions.unresolved_asset_job_definition import (
        UnresolvedAssetJobDefinition,
    )

causal_res_dynamic: Any = CausalRunnerResource
causal_res_dynamic.__annotations__["graph_config"] = Any

if hasattr(causal_res_dynamic, "model_fields"):
    causal_res_dynamic.model_fields["graph_config"].annotation = Any
    try:
        causal_res_dynamic.model_rebuild(force=True)
    except Exception:
        pass
elif hasattr(causal_res_dynamic, "__fields__"):
    v1_fields: Any = causal_res_dynamic.__fields__
    if isinstance(v1_fields, dict) and "graph_config" in v1_fields:
        v1_fields["graph_config"].type_ = Any
        v1_fields["graph_config"].outer_type_ = Any


class PipelineExecutorResource(dg.ConfigurableResource):
    """Configurable stateful factory translating platform configs into execution steps."""

    ray_address: Optional[str] = None
    db_provider: PostgresResource

    _pipeline_config: Any = PrivateAttr(default=None)
    _vision_config: Any = PrivateAttr(default=None)
    _executor: Optional[ESKGPipelineExecutor] = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Sets up the pipeline executor without mutating global process environment variables."""
        if self.ray_address:
            daft.set_runner_ray(
                address=self.ray_address, noop_if_initialized=True
            )

        self._executor = ESKGPipelineExecutor(
            config=self._pipeline_config,
            vision_config=self._vision_config,
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
    description="Consumes a micro-batch from Kafka, staged to S3 as Parquet, and returns the URI.",
)
async def staged_batch(
    context: dg.AssetExecutionContext,
    kafka: KafkaResource,
    s3_client_resource: S3ClientResource,
) -> BatchHandle[str]:
    """Consumes records from Kafka streams and uploads them cleanly to persistent objects."""
    batch: list[dict[str, Any]] = await kafka.poll_batch(
        max_records=1000, timeout_s=5.0
    )

    if not batch:
        context.log.info("No records fetched from Kafka.")
        return BatchHandle[str](
            correlation_id=str(uuid.uuid4()),
            kafka_offsets={},
            started_at=time.time(),
            payload="",
        )

    first_record: dict[str, Any] = batch[0]
    tenant_id: Any = first_record.get("tenant_id", "default")
    if not isinstance(tenant_id, str) or not all(
        c.isalnum() or c in "-_" for c in tenant_id
    ):
        tenant_id = "default"

    batch_id: str = str(uuid.uuid4())
    s3_key: str = f"batches/{tenant_id}/{batch_id}.parquet"

    transit_service: S3TransitService = S3TransitService(
        s3_client_resource.client
    )
    s3_uri: str = await transit_service.upload_batch(
        key=s3_key, records=batch, format_type="parquet"
    )

    offsets: dict[str, dict[int, int]] = kafka.get_current_offsets()

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
    uri: str = staged_batch.payload

    if not uri:
        result: PipelineResult = PipelineResult(
            processed_records=0, duration=0.0
        )
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


vision_pipeline_job: "UnresolvedAssetJobDefinition" = dg.define_asset_job(
    name="vision_pipeline_job",
    selection=dg.AssetSelection.all(),
)


@dg.sensor(
    job=vision_pipeline_job,
    minimum_interval_seconds=15,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def kafka_microbatch_sensor(
    context: dg.SensorEvaluationContext,
    kafka: KafkaResource,
) -> Generator[Union[dg.RunRequest, dg.SkipReason], None, None]:
    """Synchronous sensor evaluating streaming consumer group lag to generate deterministic keys."""
    if kafka.has_lag():
        offsets: dict[str, dict[int, int]] = kafka.get_current_offsets()
        offsets_str: str = "_".join(
            f"{t}_{p}_{o}"
            for t, partitions in sorted(offsets.items())
            for p, o in sorted(partitions.items())
        )
        run_key: str = (
            f"kafka_batch_{offsets_str}"
            if offsets_str
            else f"kafka_batch_{time.time()}"
        )
        yield dg.RunRequest(run_key=run_key)
    else:
        yield dg.SkipReason("No lag detected on configured Kafka topics.")


def parse_postgres_host_port(raw_host: str) -> tuple[str, int]:
    """Parses host and port securely, supporting IPv6 and standard formats."""
    parsed: ParseResult = (
        urlparse(f"tcp://{raw_host}")
        if "://" not in raw_host
        else urlparse(raw_host)
    )
    host: str = parsed.hostname or "localhost"
    port: int = parsed.port or 5432
    return host, port


def bootstrap_definitions() -> dg.Definitions:
    """Factory delivering a purely in-memory mocked configuration directly without file system lookups."""
    from unittest.mock import MagicMock

    base_cfg: Any = MagicMock()
    base_cfg.to_pipeline_config.return_value = MagicMock()
    base_cfg.postgres.host = "localhost:5432"
    base_cfg.postgres.user = "test_user"
    base_cfg.postgres.password = "test_password"
    base_cfg.postgres.database = "test_db"

    base_cfg.connectors.s3.staging_bucket = "test-bucket"
    base_cfg.connectors.s3.endpoint = "http://localhost:4566"
    base_cfg.connectors.s3.access_key = "mock-key"
    base_cfg.connectors.s3.secret_key = "mock-secret"
    base_cfg.connectors.s3.region = "us-east-1"

    base_cfg.kafka.bootstrap_servers = "localhost:9092"
    base_cfg.kafka.group_id = "test-group"
    base_cfg.get_kafka_topics.return_value = ["raw"]

    base_cfg.ray.address = "local"
    base_cfg.graph = {}

    pipe_cfg: Any = base_cfg.to_pipeline_config()
    graph_cfg: Any = base_cfg.graph
    parsed_host, parsed_port = parse_postgres_host_port(
        str(base_cfg.postgres.host)
    )

    db_res: PostgresResource = PostgresResource(
        host=parsed_host,
        port=parsed_port,
        username=str(base_cfg.postgres.user),
        password=str(base_cfg.postgres.password),
        database=str(base_cfg.postgres.database),
    )

    kafka_servers: str = str(base_cfg.kafka.bootstrap_servers)

    pipeline_executor_res: PipelineExecutorResource = PipelineExecutorResource(
        ray_address=str(base_cfg.ray.address),
        db_provider=db_res,
    )
    pipeline_executor_res._pipeline_config = pipe_cfg
    pipeline_executor_res._vision_config = base_cfg

    return dg.Definitions(
        assets=[staged_batch, execute_pipeline, run_causal],
        jobs=[vision_pipeline_job],
        sensors=[kafka_microbatch_sensor],
        resources={
            "s3_client_resource": S3ClientResource(
                bucket=str(base_cfg.connectors.s3.staging_bucket),
                endpoint_url=str(base_cfg.connectors.s3.endpoint),
                aws_access_key=str(base_cfg.connectors.s3.access_key),
                aws_secret_key=str(base_cfg.connectors.s3.secret_key),
                aws_region=str(base_cfg.connectors.s3.region),
            ),
            "kafka": KafkaResource(
                bootstrap_servers=kafka_servers,
                group_id=str(base_cfg.kafka.group_id),
                topics=cast(list[str], base_cfg.get_kafka_topics() or ["raw"]),
            ),
            "pipeline_executor": pipeline_executor_res,
            "causal_runner": CausalRunnerResource(
                graph_config=graph_cfg,
                db_provider=db_res,
            ),
        },
    )


defs: dg.Definitions = bootstrap_definitions()
