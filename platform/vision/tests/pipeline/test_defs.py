"""Tests for the Dagster definitions exposed by the vision gRPC code server."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Awaitable, Callable, Generator
from typing import NamedTuple, Protocol, cast
from unittest.mock import AsyncMock, MagicMock, patch

import dagster as dg
import pytest
from dagster._core.workspace.autodiscovery import (
    loadable_targets_from_python_module,
)
from galadril_pipeline.resources.kafka import KafkaResource
from galadril_pipeline.resources.postgres import PostgresResource
from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult
from galadril_vision.common.config import VisionConfig

_DEFS_MODULE = "galadril_vision.pipeline.defs"
_CONFIG_PATH = "/deployment/pipeline.yaml"


class PipelineExecutorResourceInstance(Protocol):
    """Operations exposed by the pipeline executor Dagster resource."""

    def setup_for_execution(self, context: dg.InitResourceContext) -> None: ...

    async def execute(self, uri: str) -> PipelineResult: ...


class PipelineExecutorResourceFactory(Protocol):
    """Constructor accepted by the production pipeline resource."""

    def __call__(
        self,
        *,
        config_path: str,
        db_provider: PostgresResource,
        ray_address: str | None = None,
    ) -> PipelineExecutorResourceInstance: ...


class CausalRunnerResourceInstance(Protocol):
    """Operations exposed by the causal Dagster resource."""

    def setup_for_execution(self, context: dg.InitResourceContext) -> None: ...


class CausalRunnerResourceFactory(Protocol):
    """Constructor accepted by the production causal resource."""

    def __call__(
        self,
        *,
        config_path: str,
        db_provider: PostgresResource,
    ) -> CausalRunnerResourceInstance: ...


class DefinitionsModule(Protocol):
    """Typed view over the production definitions module used by the tests."""

    defs: dg.Definitions
    staged_batch: dg.AssetsDefinition
    execute_pipeline: dg.AssetsDefinition
    run_causal: dg.AssetsDefinition
    kafka_microbatch_sensor: dg.SensorDefinition
    PipelineExecutorResource: PipelineExecutorResourceFactory
    CausalRunnerResource: CausalRunnerResourceFactory

    def bootstrap_definitions(self) -> dg.Definitions: ...

    def parse_postgres_host_port(self, raw_host: str) -> tuple[str, int]: ...


class ConfigPathResource(Protocol):
    """Typed view over resources reconstructed from a deployment path."""

    config_path: str


class DecoratedComputeFunction(Protocol):
    """Typed access to the original callable wrapped by a Dagster asset."""

    decorated_fn: object


class LoadedDefinitions(NamedTuple):
    """Holds the imported module and its patched deployment context."""

    module: DefinitionsModule
    config: VisionConfig
    configure_runtime: MagicMock


def _vision_config() -> VisionConfig:
    """Builds a complete deployment configuration without external services."""
    return VisionConfig.model_validate(
        {
            "name": "vision-test",
            "connectors": {
                "kafka": {
                    "brokers": ["kafka-a:9092", "kafka-b:9092"],
                    "schema_registry": "http://schema-registry:8081",
                    "consumer_group": "vision-tests",
                },
                "s3": {
                    "endpoint": "http://minio:9000",
                    "access_key": "access",
                    "secret_key": "secret",
                    "region": "eu-west-3",
                    "bucket": "raw",
                    "staging_bucket": "staging",
                },
                "postgres": {
                    "database": "vision",
                    "host": "postgres.internal:6432",
                    "user": "vision",
                    "password": "secret",
                },
                "spicedb": {
                    "endpoint": "spicedb:50051",
                    "token": "token",
                },
            },
            "sources": [
                {
                    "id": "raw-events",
                    "topic": "raw",
                    "match_pattern": ".*",
                    "schema_path": "schemas/raw.avsc",
                }
            ],
            "ray": {"address": "ray://ray-head:10001"},
        }
    )


@pytest.fixture(scope="module")
def loaded_definitions() -> Generator[LoadedDefinitions, None, None]:
    """Imports the real code-server module under an isolated deployment config."""
    config = _vision_config()
    sys.modules.pop(_DEFS_MODULE, None)

    with (
        patch.dict(os.environ, {"PIPELINE_PATH": _CONFIG_PATH}),
        patch.object(VisionConfig, "from_yaml", return_value=config),
        patch("galadril_vision.runtime.configure_runtime") as configure_runtime,
    ):
        imported = importlib.import_module(_DEFS_MODULE)
        module = cast(DefinitionsModule, imported)
        yield LoadedDefinitions(module, config, configure_runtime)

    sys.modules.pop(_DEFS_MODULE, None)


def _postgres_resource() -> tuple[PostgresResource, MagicMock]:
    """Creates an initialized database resource without opening a connection."""
    pg_client = MagicMock()
    resource = PostgresResource(
        host="postgres.internal",
        port=6432,
        username="vision",
        password="secret",
        database="vision",
    )
    resource._client = pg_client
    return resource, pg_client


def _decorated_function(asset: dg.AssetsDefinition) -> object:
    """Returns the original asset callable after asserting decorator shape."""
    compute_function = cast(DecoratedComputeFunction, asset.op.compute_fn)
    return compute_function.decorated_fn


def test_grpc_loader_discovers_single_definitions_target(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Uses Dagster's gRPC loader path to validate the exported module contract."""
    targets = loadable_targets_from_python_module(
        _DEFS_MODULE,
        working_directory=None,
    )

    assert len(targets) == 1
    assert targets[0].attribute == "defs"
    assert targets[0].target_definition is loaded_definitions.module.defs


def test_definitions_expose_expected_topology_and_resources(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Validates assets, job, sensor, and worker-safe resource configuration."""
    definitions = loaded_definitions.module.defs
    resources = definitions.resources
    assert resources is not None

    assert {
        key.to_user_string() for key in definitions.resolve_all_asset_keys()
    } == {"staged_batch", "execute_pipeline", "run_causal"}
    assert definitions.get_job_def("vision_pipeline_job").name == (
        "vision_pipeline_job"
    )
    assert definitions.get_sensor_def("kafka_microbatch_sensor").name == (
        "kafka_microbatch_sensor"
    )
    assert set(resources) == {
        "s3_client_resource",
        "kafka",
        "pipeline_executor",
        "causal_runner",
    }

    kafka = cast(KafkaResource, resources["kafka"])
    pipeline_executor = cast(
        ConfigPathResource,
        resources["pipeline_executor"],
    )
    causal_runner = cast(
        ConfigPathResource,
        resources["causal_runner"],
    )

    assert kafka.bootstrap_servers == "kafka-a:9092,kafka-b:9092"
    assert kafka.topics == ["raw"]
    assert pipeline_executor.config_path == _CONFIG_PATH
    assert causal_runner.config_path == _CONFIG_PATH
    loaded_definitions.configure_runtime.assert_called_once_with(
        loaded_definitions.config
    )


@pytest.mark.parametrize(
    ("raw_host", "expected"),
    [
        ("postgres", ("postgres", 5432)),
        ("postgres:6432", ("postgres", 6432)),
        ("postgresql://postgres:7432/vision", ("postgres", 7432)),
        ("[2001:db8::1]:8432", ("2001:db8::1", 8432)),
    ],
)
def test_parse_postgres_host_port(
    loaded_definitions: LoadedDefinitions,
    raw_host: str,
    expected: tuple[str, int],
) -> None:
    """Covers host forms accepted by the deployment configuration."""
    assert loaded_definitions.module.parse_postgres_host_port(raw_host) == (
        expected
    )


def test_pipeline_executor_resource_builds_from_config_path(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Ensures Dagster workers reconstruct executor state from a primitive path."""
    module = loaded_definitions.module
    db_provider, pg_client = _postgres_resource()
    resource = module.PipelineExecutorResource(
        config_path=_CONFIG_PATH,
        ray_address="ray://ray-head:10001",
        db_provider=db_provider,
    )

    with (
        patch(f"{_DEFS_MODULE}.daft.set_runner_ray") as set_runner,
        patch(f"{_DEFS_MODULE}.ESKGPipelineExecutor") as executor_class,
    ):
        resource.setup_for_execution(MagicMock(spec=dg.InitResourceContext))

    set_runner.assert_called_once_with(
        address="ray://ray-head:10001",
        noop_if_initialized=True,
    )
    executor_class.assert_called_once_with(
        config=loaded_definitions.config.to_pipeline_config(),
        vision_config=loaded_definitions.config,
        pg_client=pg_client,
    )


@pytest.mark.asyncio
async def test_pipeline_executor_resource_rejects_use_before_setup(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Prevents asset execution with an uninitialized worker resource."""
    db_provider, _ = _postgres_resource()
    resource = loaded_definitions.module.PipelineExecutorResource(
        config_path=_CONFIG_PATH,
        db_provider=db_provider,
    )

    with pytest.raises(
        RuntimeError,
        match="PipelineExecutorResource accessed before setup",
    ):
        await resource.execute("s3://staging/batch.parquet")


def test_causal_resource_builds_from_config_path(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Ensures causal execution receives the validated PostgreSQL settings."""
    module = loaded_definitions.module
    db_provider, pg_client = _postgres_resource()
    resource = module.CausalRunnerResource(
        config_path=_CONFIG_PATH,
        db_provider=db_provider,
    )

    with (
        patch(f"{_DEFS_MODULE}.GraphStore") as graph_store,
        patch(f"{_DEFS_MODULE}.AmarthCausalRunner") as runner_class,
    ):
        resource.setup_for_execution(MagicMock(spec=dg.InitResourceContext))

    graph_store.assert_called_once_with(
        config=loaded_definitions.config.postgres,
        client=pg_client,
    )
    runner_class.assert_called_once_with(
        pg=pg_client,
        graph=graph_store.return_value,
    )


@pytest.mark.asyncio
async def test_staged_batch_returns_empty_handle_without_s3_write(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Skips storage allocation when Kafka has no records."""
    kafka = MagicMock()
    kafka.poll_batch = AsyncMock(return_value=[])
    s3_resource = MagicMock()
    context = MagicMock(spec=dg.AssetExecutionContext)
    staged_fn = cast(
        Callable[
            [MagicMock, MagicMock, MagicMock],
            Awaitable[BatchHandle[str]],
        ],
        _decorated_function(loaded_definitions.module.staged_batch),
    )

    result = await staged_fn(context, kafka, s3_resource)

    assert result.payload == ""
    assert result.kafka_offsets == {}
    kafka.get_current_offsets.assert_not_called()
    context.add_output_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_staged_batch_uploads_tenant_scoped_parquet(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Stages a Kafka micro-batch and preserves offsets for later commit."""
    records = [{"tenant_id": "tenant-42", "payload": {"value": 1}}]
    offsets = {"raw": {0: 18}}
    kafka = MagicMock()
    kafka.poll_batch = AsyncMock(return_value=records)
    kafka.get_current_offsets.return_value = offsets
    s3_resource = MagicMock()
    context = MagicMock(spec=dg.AssetExecutionContext)
    transit = MagicMock()
    transit.upload_batch = AsyncMock(
        return_value="s3://staging/batches/tenant-42/batch-id.parquet"
    )
    staged_fn = cast(
        Callable[
            [MagicMock, MagicMock, MagicMock],
            Awaitable[BatchHandle[str]],
        ],
        _decorated_function(loaded_definitions.module.staged_batch),
    )

    with (
        patch(f"{_DEFS_MODULE}.S3TransitService", return_value=transit),
        patch(f"{_DEFS_MODULE}.uuid.uuid4", return_value="batch-id"),
    ):
        result = await staged_fn(context, kafka, s3_resource)

    transit.upload_batch.assert_awaited_once_with(
        key="batches/tenant-42/batch-id.parquet",
        records=records,
        format_type="parquet",
    )
    assert result.correlation_id == "batch-id"
    assert result.kafka_offsets == offsets
    assert result.payload == ("s3://staging/batches/tenant-42/batch-id.parquet")
    context.add_output_metadata.assert_called_once_with(
        {
            "staged_parquet_uri": result.payload,
            "records_ingested": 1,
        }
    )


@pytest.mark.asyncio
async def test_execute_pipeline_short_circuits_empty_batch(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Avoids initializing distributed compute for an empty Kafka poll."""
    source = BatchHandle[str](
        correlation_id="batch-id",
        kafka_offsets={},
        started_at=10.0,
        payload="",
    )
    executor = MagicMock()
    executor.execute = AsyncMock()
    context = MagicMock(spec=dg.AssetExecutionContext)
    execute_fn = cast(
        Callable[
            [MagicMock, BatchHandle[str], MagicMock],
            Awaitable[BatchHandle[PipelineResult]],
        ],
        _decorated_function(loaded_definitions.module.execute_pipeline),
    )

    result = await execute_fn(context, source, executor)

    executor.execute.assert_not_awaited()
    assert result.payload == PipelineResult(
        processed_records=0,
        duration=0.0,
    )
    context.add_output_metadata.assert_called_once_with(
        {"processed_records": 0, "duration_seconds": 0.0}
    )


@pytest.mark.asyncio
async def test_run_causal_commits_offsets_after_success(
    loaded_definitions: LoadedDefinitions,
) -> None:
    """Commits Kafka offsets only after downstream causal work completes."""
    batch = BatchHandle[PipelineResult](
        correlation_id="batch-id",
        kafka_offsets={"raw": {0: 18}},
        started_at=10.0,
        payload=PipelineResult(processed_records=3, duration=0.25),
    )
    causal_runner = MagicMock()
    causal_runner.run = AsyncMock(return_value={"status": "processed"})
    kafka = MagicMock()
    kafka.commit_offsets = AsyncMock()
    context = MagicMock(spec=dg.AssetExecutionContext)
    causal_fn = cast(
        Callable[
            [MagicMock, BatchHandle[PipelineResult], MagicMock, MagicMock],
            Awaitable[BatchHandle[PipelineResult]],
        ],
        _decorated_function(loaded_definitions.module.run_causal),
    )

    result = await causal_fn(context, batch, causal_runner, kafka)

    assert result is batch
    causal_runner.run.assert_awaited_once_with(batch=batch)
    kafka.commit_offsets.assert_awaited_once_with({"raw": {0: 18}})


@pytest.mark.parametrize(
    ("has_lag", "offsets", "expected_run_key", "expected_skip"),
    [
        (
            True,
            {"raw": {1: 12, 0: 8}},
            "kafka_batch_raw_0_8_raw_1_12",
            None,
        ),
        (False, {}, None, "No lag detected on configured Kafka topics."),
    ],
)
def test_kafka_sensor_emits_deterministic_decision(
    loaded_definitions: LoadedDefinitions,
    has_lag: bool,
    offsets: dict[str, dict[int, int]],
    expected_run_key: str | None,
    expected_skip: str | None,
) -> None:
    """Validates stable run de-duplication keys and idle skip behavior."""
    kafka = MagicMock(spec=KafkaResource)
    kafka.has_lag.return_value = has_lag
    kafka.get_current_offsets.return_value = offsets

    with dg.build_sensor_context(resources={"kafka": kafka}) as context:
        result = (
            loaded_definitions.module.kafka_microbatch_sensor.evaluate_tick(
                context
            )
        )

    if expected_run_key is not None:
        assert [request.run_key for request in (result.run_requests or [])] == [
            expected_run_key
        ]
        assert result.skip_message is None
    else:
        assert result.run_requests == []
        assert result.skip_message == expected_skip
