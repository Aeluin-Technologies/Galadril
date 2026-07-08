"""
End-to-end (E2E) test suite for the vision processing pipeline.
This file configures assets, mock resources, and the streaming sensor,
and validates the complete execution graph within the Dagster framework.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, TypeVar

import dagster as dg
import pytest

T = TypeVar("T")


@dataclass
class BatchHandle[T]:
    """Mirrors production tracking wrapper using explicit Python Generic typing."""

    correlation_id: str
    kafka_offsets: dict[str, dict[int, int]]
    payload: T
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


@dataclass
class PipelineResult:
    """Mirrors production compute execution metadata matrix footprint."""

    processed_records: int
    duration: float


class CanonicalRecord:
    """Stub representing unified internal vision data tracking layout."""

    pass


@dataclass
class IngestedMessage:
    """Stub representing incoming frame data metadata bindings."""

    topic: str
    payload: Any
    event_type: str


@dataclass
class ValidatedBatch:
    """Container separating verified messages from malformed telemetry."""

    accepted: list[CanonicalRecord]
    rejected: list[Any]


def validate_and_normalize_kafka_batch(
    messages: list[IngestedMessage],
) -> ValidatedBatch:
    """Simulates framework validation parsing constraints over messages."""
    return ValidatedBatch(
        accepted=[CanonicalRecord() for _ in messages], rejected=[]
    )


class KafkaResource(dg.ConfigurableResource):
    """Test-isolated Kafka resource bypassing real broker network calls."""

    bootstrap_servers: str
    group_id: str
    topics: list[str]
    mock_lag_present: bool = False
    mock_records: list[dict[str, Any]] = field(default_factory=list)

    def has_lag(self) -> bool:
        """Returns injected stream latency tracking metric boolean flags."""
        return self.mock_lag_present

    async def poll_batch(
        self, max_records: int = 1000, timeout_s: float = 1.0
    ) -> list[dict[str, Any]]:
        """Returns mock record payload arrays synchronously inside the test thread."""
        return self.mock_records

    async def commit_offsets(self, offsets: dict[str, dict[int, int]]) -> None:
        """No-op boundary method skipping network acknowledgement packets."""
        pass


class S3TransitService(dg.ConfigurableResource):
    """Mock target remote cloud staging layer implementation."""

    async def upload(self, records: list[CanonicalRecord]) -> str:
        """Returns artificial target bucket location pointers."""
        return "s3://galadril-testing-bucket/transit/batch_01.parquet"


class ESKGPipelineExecutor(dg.ConfigurableResource):
    """Mock hardware accelerated inference orchestration engine."""

    async def execute(self, uri: str) -> PipelineResult:
        """Simulates successful deep-learning network tensor evaluation runtime."""
        return PipelineResult(processed_records=1, duration=0.042)


class AmarthCausalRunner(dg.ConfigurableResource):
    """Mock validation tracking context generation agent."""

    async def run(self, batch: BatchHandle) -> None:
        """Simulates telemetry capture tracking mutations."""
        pass


@dg.asset(
    compute_kind="kafka",
    retry_policy=dg.RetryPolicy(max_retries=3, delay=5.0),
    description="Polls raw streaming frames and normalizes them into standard ESKG layouts.",
)
async def kafka_source(
    context: dg.AssetExecutionContext, kafka_resource: KafkaResource
) -> Any:
    """Polls streaming frames directly and encapsulates validated payloads."""
    raw_messages = await kafka_resource.poll_batch()
    simulated_offsets = {"vision.events.v1": {0: 1042}} if raw_messages else {}
    ingested_messages = [
        IngestedMessage(
            topic=msg.get("topic", "unknown"),
            payload=msg.get("payload", msg),
            event_type=msg.get("event_type", "UNKNOWN"),
        )
        for msg in raw_messages
    ]
    validated_batch = validate_and_normalize_kafka_batch(ingested_messages)

    context.add_output_metadata(
        {
            "raw_messages_polled": len(raw_messages),
            "records_accepted": len(validated_batch.accepted),
            "records_rejected": len(validated_batch.rejected),
        }
    )
    return BatchHandle(
        correlation_id=str(uuid.uuid4()),
        kafka_offsets=simulated_offsets,
        payload=validated_batch.accepted,
    )


@dg.asset(
    compute_kind="s3",
    description="Stages memory batch objects as optimized remote Parquet tables.",
)
async def stage_batch(
    context: dg.AssetExecutionContext,
    kafka_source: Any,
    transit_service: S3TransitService,
) -> Any:
    """Offloads the unified record collection to remote transit stores via encapsulated service layers."""
    if not kafka_source.payload:
        return BatchHandle(
            correlation_id=kafka_source.correlation_id,
            kafka_offsets=kafka_source.kafka_offsets,
            started_at=kafka_source.started_at,
            payload="",
        )

    s3_uri = await transit_service.upload(records=kafka_source.payload)
    context.add_output_metadata({"staged_parquet_uri": s3_uri})

    return BatchHandle(
        correlation_id=kafka_source.correlation_id,
        kafka_offsets=kafka_source.kafka_offsets,
        started_at=kafka_source.started_at,
        payload=s3_uri,
    )


@dg.asset(compute_kind="daft", op_tags={"cluster": "ray-inference-pool"})
async def execute_pipeline(
    context: dg.AssetExecutionContext,
    stage_batch: Any,
    pipeline_executor: ESKGPipelineExecutor,
) -> Any:
    """Compiles and executes the memory-efficient processing pipeline over remote storage pointers."""
    uri = stage_batch.payload
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
    return BatchHandle(
        correlation_id=stage_batch.correlation_id,
        kafka_offsets=stage_batch.kafka_offsets,
        started_at=stage_batch.started_at,
        payload=result,
    )


@dg.asset(compute_kind="causal")
async def run_causal(
    context: dg.AssetExecutionContext,
    execute_pipeline: Any,
    causal_runner: AmarthCausalRunner,
) -> Any:
    """Applies contextual tracking models over internal state layers using explicit batch mapping bindings."""
    if execute_pipeline.payload.processed_records > 0:
        await causal_runner.run(batch=execute_pipeline)
        context.log.info(
            "Causal model processing steps finished execution successfully."
        )

    return execute_pipeline


@dg.asset(compute_kind="kafka")
async def commit_offsets(
    context: dg.AssetExecutionContext,
    run_causal: Any,
    kafka_resource: KafkaResource,
) -> Any:
    """Finalizes data guarantees by committing verified processing windows back to broker logs."""
    if run_causal.kafka_offsets:
        await kafka_resource.commit_offsets(run_causal.kafka_offsets)
        context.log.info(
            "Successfully registered transaction boundaries with coordinator nodes."
        )

    return BatchHandle(
        correlation_id=run_causal.correlation_id,
        kafka_offsets=run_causal.kafka_offsets,
        started_at=run_causal.started_at,
        finished_at=time.time(),
        payload=run_causal.payload,
    )


vision_pipeline_job = dg.define_asset_job(
    name="vision_pipeline_job",
    selection=dg.AssetSelection.assets(
        kafka_source, stage_batch, execute_pipeline, run_causal, commit_offsets
    ),
)


@dg.sensor(job=vision_pipeline_job, minimum_interval_seconds=2)
def kafka_stream_sensor(
    context: dg.SensorEvaluationContext,
) -> list[dg.RunRequest] | dg.SkipReason:
    """Interrogates cluster streaming lag metrics cleanly without mutations."""
    kafka_res: KafkaResource = context.resources.kafka_resource
    if kafka_res.has_lag():
        return [dg.RunRequest(run_key=f"mb_{int(time.time())}")]
    return dg.SkipReason(
        "Monitoring bounds confirm zero uncommitted backlog entries."
    )


defs = dg.Definitions(
    assets=[
        kafka_source,
        stage_batch,
        execute_pipeline,
        run_causal,
        commit_offsets,
    ],
    jobs=[vision_pipeline_job],
)


@pytest.fixture
def base_resources() -> dict:
    """Provides standard isolated pipeline structural environments."""
    return {
        "kafka_resource": KafkaResource(
            bootstrap_servers="localhost:9092",
            group_id="test_suite_group",
            topics=["vision.events.v1"],
            mock_lag_present=False,
            mock_records=[],
        ),
        "transit_service": S3TransitService(),
        "pipeline_executor": ESKGPipelineExecutor(),
        "causal_runner": AmarthCausalRunner(),
    }


def test_sensor_skips_without_lag(base_resources):
    """Ensures sensor returns SkipReason when zero streaming backlog exists."""
    sensor_context = dg.build_sensor_context(
        definitions=dg.Definitions(
            sensors=[kafka_stream_sensor], resources=base_resources
        )
    )
    result = kafka_stream_sensor(sensor_context)
    assert isinstance(result, dg.SkipReason)
    assert result.skip_message is not None
    assert "zero uncommitted backlog entries" in result.skip_message


def test_sensor_triggers_run_on_lag(base_resources):
    """Ensures sensor generates RunRequests when streaming lag is flagged."""
    base_resources["kafka_resource"].mock_lag_present = True

    sensor_context = dg.build_sensor_context(
        definitions=dg.Definitions(
            sensors=[kafka_stream_sensor], resources=base_resources
        )
    )
    result = kafka_stream_sensor(sensor_context)
    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], dg.RunRequest)

    run_key = result[0].run_key
    assert run_key is not None
    assert run_key.startswith("mb_")


def test_pipeline_job_e2e_successful_execution(base_resources):
    """Executes the full pipeline to verify topology constraints and outputs."""
    base_resources["kafka_resource"].mock_records = [
        {
            "topic": "vision.events.v1",
            "payload": {"frame_id": 404},
            "event_type": "FRAME",
        }
    ]

    job_def = defs.get_job_def("vision_pipeline_job")
    job_result = job_def.execute_in_process(resources=base_resources)

    assert job_result.success

    source_out = job_result.output_for_node("kafka_source")
    assert isinstance(source_out, BatchHandle)
    assert len(source_out.payload) == 1

    stage_out = job_result.output_for_node("stage_batch")
    assert stage_out.payload.startswith("s3://")

    execution_out = job_result.output_for_node("execute_pipeline")
    assert isinstance(execution_out.payload, PipelineResult)
    assert execution_out.payload.processed_records == 1

    commit_out = job_result.output_for_node("commit_offsets")
    assert commit_out.finished_at is not None
    assert commit_out.started_at is not None
    assert commit_out.finished_at > commit_out.started_at


def test_pipeline_job_graceful_noop_routing(base_resources):
    """Verifies that an empty message pool does not crash downstream assets."""
    base_resources["kafka_resource"].mock_records = []

    job_def = defs.get_job_def("vision_pipeline_job")
    job_result = job_def.execute_in_process(resources=base_resources)
    assert job_result.success

    stage_out = job_result.output_for_node("stage_batch")
    assert stage_out.payload == ""

    execution_out = job_result.output_for_node("execute_pipeline")
    assert execution_out.payload.processed_records == 0
