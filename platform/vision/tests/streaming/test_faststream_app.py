"""Unit tests for FastStream application wiring and Avro decoding."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from confluent_kafka import Message
from confluent_kafka.schema_registry import AsyncSchemaRegistryClient
from faststream.message import StreamMessage
from galadril_pipeline.events import ResourceClass
from galadril_vision.common.config import VisionConfig
from galadril_vision.streaming.app import (
    ServiceRole,
    _gpu_actor_requirement,
    _initialize_ray,
    _Runtime,
    _verify_ray_runtime,
    build_stream_app,
    faststream_logger,
)
from galadril_vision.streaming.codec import (
    AvroMessageDecoder,
    _pipeline_identity,
)


class _Registry:
    """Records Schema Registry lifecycle calls for the decoder."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def anyio_backend() -> str:
    """Runs decoder tests on the production asyncio backend."""
    return "asyncio"


class _Resolver:
    """Resolves every test schema to one configured source."""

    def __init__(self) -> None:
        self.registry = _Registry()
        self.registry_client = cast(AsyncSchemaRegistryClient, self.registry)

    async def resolve_event_type(self, raw_bytes: bytes) -> str:
        assert raw_bytes == b"avro-wire"
        return "image_source"

    async def close(self) -> None:
        await self.registry.close()


class _Deserializer:
    """Returns an Avro logical timestamp to exercise JSON normalization."""

    async def __call__(self, data: bytes, context: object) -> dict[str, object]:
        del context
        assert data == b"avro-wire"
        return {
            "id": "record-1",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }


class _RawMessage:
    """Provides the raw topic method used by the Confluent decoder."""

    def topic(self) -> str:
        return "raw"

    def headers(self) -> list[tuple[str, bytes]]:
        return [
            ("galadril-tenant-id", b"tenant_a"),
            ("galadril-pipeline-id", b"daily"),
            ("galadril-pipeline-revision", b"revision_a"),
        ]


class _HeadersMessage:
    """Provides caller-selected Kafka headers for fail-closed tests."""

    def __init__(self, headers: list[tuple[str, bytes | None]]) -> None:
        self._headers = headers

    def headers(self) -> list[tuple[str, bytes | None]]:
        return self._headers


async def _deserializer_factory(
    client: AsyncSchemaRegistryClient,
) -> _Deserializer:
    assert client is not None
    return _Deserializer()


def _config() -> VisionConfig:
    """Creates a minimal application configuration with real schema paths."""
    return VisionConfig.model_validate(
        {
            "name": "vision",
            "connectors": {
                "kafka": {
                    "brokers": ["localhost:9092"],
                    "schema_registry": "http://localhost:8081",
                    "consumer_group": "test",
                },
                "s3": {
                    "endpoint": "http://localhost:9000",
                    "access_key": "test",
                    "secret_key": "test",
                    "region": "us-east-1",
                    "bucket": "raw",
                },
                "postgres": {
                    "database": "test",
                    "host": "localhost:5432",
                    "user": "test",
                    "password": "test",
                },
                "spicedb": {
                    "endpoint": "localhost:50051",
                    "token": "test",
                },
            },
            "sources": [
                {
                    "id": "image_source",
                    "topic": "raw",
                    "match_pattern": ".*",
                    "schema_path": "schemas/avro/image.avsc",
                }
            ],
            "pipeline": [
                {
                    "step": "infer",
                    "type": "inference",
                    "model": "models.Face",
                    "input_from": ["image_source"],
                }
            ],
        }
    )


@pytest.mark.anyio
async def test_avro_decoder_normalizes_logical_types_once() -> None:
    """Ensures decoded Avro enters Pydantic as a plain JSON contract."""
    resolver = _Resolver()
    decoder = AvroMessageDecoder(
        sources=[],
        schema_registry_url="http://unused",
        resolver=resolver,
        deserializer_factory=_deserializer_factory,
    )
    message = StreamMessage(
        raw_message=_RawMessage(),
        body=b"avro-wire",
    )

    decoded = await decoder(message)
    await decoder.close()

    assert decoded["source_id"] == "image_source"
    assert decoded["tenant_id"] == "tenant_a"
    assert decoded["pipeline_id"] == "daily"
    assert decoded["revision_id"] == "revision_a"
    payload = decoded["payload"]
    assert isinstance(payload, dict)
    assert payload["timestamp"] == "2026-01-01T00:00:00+00:00"
    assert resolver.registry.closed is True


@pytest.mark.parametrize(
    "headers, message",
    [
        ([], "missing immutable pipeline identity"),
        (
            [
                ("galadril-tenant-id", b"tenant_a"),
                ("galadril-tenant-id", b"tenant_b"),
                ("galadril-pipeline-id", b"daily"),
                ("galadril-pipeline-revision", b"revision_a"),
            ],
            "duplicated",
        ),
        (
            [
                ("galadril-tenant-id", b"\xff"),
                ("galadril-pipeline-id", b"daily"),
                ("galadril-pipeline-revision", b"revision_a"),
            ],
            "not UTF-8",
        ),
        (
            [
                ("galadril-tenant-id", None),
                ("galadril-pipeline-id", b"daily"),
                ("galadril-pipeline-revision", b"revision_a"),
            ],
            "empty",
        ),
    ],
)
def test_pipeline_identity_headers_fail_closed(
    headers: list[tuple[str, bytes | None]], message: str
) -> None:
    """Rejects incomplete, ambiguous, or malformed immutable identities."""
    raw_message = cast(Message, _HeadersMessage(headers))

    with pytest.raises(ValueError, match=message):
        _pipeline_identity(raw_message)


def test_app_registers_role_specific_and_unified_subscribers() -> None:
    """Supports split KubeRay deployments and one unified local process."""
    ingress = build_stream_app(
        _config(),
        role=ServiceRole.INGRESS,
    )
    gpu = build_stream_app(
        _config(),
        role=ServiceRole.GPU,
    )
    unified = build_stream_app(
        _config(),
        role=ServiceRole.ALL,
    )

    assert ingress.broker is not None
    assert gpu.broker is not None
    assert unified.broker is not None
    assert len(ingress.broker._subscribers) == 1
    assert len(gpu.broker._subscribers) == 1
    assert len(unified.broker._subscribers) == 4
    assert ingress.logger is faststream_logger
    assert isinstance(ingress.logger, logging.Logger)


@pytest.mark.anyio
async def test_app_lifespan_owns_authz_worker_lifecycle() -> None:
    """Keeps outbox startup on the lifecycle path used by FastStream run()."""
    start = AsyncMock()
    start_background = AsyncMock()
    stop_background = AsyncMock()
    close = AsyncMock()

    with (
        patch.object(_Runtime, "start", start),
        patch.object(_Runtime, "start_background", start_background),
        patch.object(_Runtime, "stop_background", stop_background),
        patch.object(_Runtime, "close", close),
    ):
        app = build_stream_app(_config(), role=ServiceRole.CPU)
        async with app.lifespan_context():
            start.assert_awaited_once()
            start_background.assert_awaited_once()

    stop_background.assert_awaited_once_with()
    close.assert_awaited_once_with()


def test_ray_initializes_process_local_runtime_without_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starts embedded Ray when neither environment nor YAML has an address."""
    ray_module = MagicMock()
    ray_module.is_initialized.return_value = False
    monkeypatch.delenv("RAY_ADDRESS", raising=False)

    with patch.dict(sys.modules, {"ray": ray_module}):
        started = _initialize_ray(_config())

    assert started is True
    ray_module.init.assert_called_once_with(
        address=None,
        num_cpus=None,
        num_gpus=None,
        namespace="galadril",
        include_dashboard=False,
        ignore_reinit_error=True,
        log_to_driver=False,
        _system_config={"agent_register_timeout_ms": 120_000},
    )


def test_ray_readiness_requires_two_successful_driver_probes() -> None:
    """Prevents a newly started raylet from exposing broker consumers early."""
    ray_module = MagicMock()
    ray_module.remote.return_value.remote.side_effect = ("first", "second")
    ray_module.get.side_effect = (True, True)

    with patch("galadril_vision.streaming.app.time.sleep") as sleep:
        _verify_ray_runtime(ray_module)

    assert ray_module.remote.call_count == 2
    assert ray_module.remote.return_value.remote.call_count == 2
    assert ray_module.get.call_args_list == [
        (("first",), {}),
        (("second",), {}),
    ]
    sleep.assert_called_once_with(5.0)


@pytest.mark.anyio
async def test_runtime_initializes_embedded_ray_on_the_main_thread() -> None:
    """Keeps Ray's process and signal lifecycle on its owning main thread."""
    runtime = _Runtime(
        config=_config(),
        resources=(ResourceClass.CPU,),
        registry=MagicMock(configs=()),
        topics=MagicMock(),
        metrics=MagicMock(),
    )
    postgres = MagicMock()
    postgres.connect = AsyncMock()

    with (
        patch(
            "galadril_vision.streaming.app.PostgresClient",
            return_value=postgres,
        ),
        patch(
            "galadril_vision.streaming.app._initialize_ray",
            return_value=True,
        ) as initialize_ray,
        patch(
            "galadril_vision.streaming.app._create_actor_pool",
            return_value=(MagicMock(),),
        ),
        patch("galadril_vision.streaming.app.asyncio.to_thread") as to_thread,
    ):
        await runtime.start(MagicMock())

    initialize_ray.assert_called_once_with(runtime.config)
    to_thread.assert_not_called()


@pytest.mark.anyio
async def test_authz_worker_acquires_maintenance_connection_before_ready() -> (
    None
):
    """Prevents an unobserved background connection failure after startup."""
    runtime = _Runtime(
        config=_config(),
        resources=(ResourceClass.CPU,),
        registry=MagicMock(configs=()),
        topics=MagicMock(),
        metrics=MagicMock(),
    )
    connection = MagicMock()
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    postgres = MagicMock()
    postgres.maintenance_connection.return_value = connection_context
    runtime.postgres = postgres

    async def run_until_stopped(
        *, conn: object, stop_event: asyncio.Event
    ) -> None:
        assert conn is connection
        await stop_event.wait()

    flusher = MagicMock()
    flusher.run_forever = AsyncMock(side_effect=run_until_stopped)

    with patch(
        "galadril_vision.streaming.app.AuthzOutboxFlusher",
        return_value=flusher,
    ):
        await runtime.start_background(MagicMock())
        postgres.maintenance_connection.assert_called_once_with()
        connection_context.__aenter__.assert_awaited_once_with()
        assert runtime.authz_task is not None
        assert not runtime.authz_task.done()
        await runtime.stop_background()

    flusher.run_forever.assert_awaited_once_with(
        conn=connection,
        stop_event=runtime.authz_stop,
    )
    connection_context.__aexit__.assert_awaited_once()


def test_ray_connects_to_environment_cluster_without_local_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uses the KubeRay endpoint without starting local workers or dashboard."""
    ray_module = MagicMock()
    ray_module.is_initialized.return_value = False
    monkeypatch.setenv("RAY_ADDRESS", "ray://vision-ray-head-svc:10001")

    with patch.dict(sys.modules, {"ray": ray_module}):
        started = _initialize_ray(_config())

    assert started is True
    ray_module.init.assert_called_once_with(
        address="ray://vision-ray-head-svc:10001",
        namespace="galadril",
        ignore_reinit_error=True,
        log_to_driver=False,
    )


def test_empty_environment_uses_configured_cluster_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treats an empty Compose variable as absent instead of disabling YAML."""
    ray_module = MagicMock()
    ray_module.is_initialized.return_value = False
    config = _config()
    config.ray.address = "ray://configured-ray-head-svc:10001"
    monkeypatch.setenv("RAY_ADDRESS", "")

    with patch.dict(sys.modules, {"ray": ray_module}):
        started = _initialize_ray(config)

    assert started is True
    ray_module.init.assert_called_once_with(
        address="ray://configured-ray-head-svc:10001",
        namespace="galadril",
        ignore_reinit_error=True,
        log_to_driver=False,
    )


def test_local_gpu_actor_uses_cpu_when_ray_detects_no_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps the single Compose worker runnable on CPU-only small systems."""
    ray_module = MagicMock()
    ray_module.cluster_resources.return_value = {"CPU": 4.0}
    monkeypatch.delenv("RAY_ADDRESS", raising=False)

    with patch.dict(sys.modules, {"ray": ray_module}):
        requirement = _gpu_actor_requirement(_config())

    assert requirement == 0.0


def test_cluster_gpu_actor_requests_autoscalable_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creates GPU resource demand when dispatching against KubeRay."""
    monkeypatch.setenv("RAY_ADDRESS", "ray://vision-ray-head-svc:10001")

    assert _gpu_actor_requirement(_config()) == 1.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
