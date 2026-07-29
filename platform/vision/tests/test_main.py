"""Tests for the galadril-vision Dagster gRPC service entry point."""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Awaitable, Callable, Coroutine
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.common.config import VisionConfig
from galadril_vision.main import (
    _build_dagster_grpc_command,
    _terminate_code_server,
    _valid_grpc_port,
    main,
)


def _vision_config() -> VisionConfig:
    """Builds the minimum validated service configuration."""
    return VisionConfig.model_validate(
        {
            "name": "vision-test",
            "connectors": {
                "kafka": {
                    "brokers": ["kafka:9092"],
                    "schema_registry": "http://schema-registry:8081",
                    "consumer_group": "vision-tests",
                },
                "s3": {
                    "endpoint": "http://minio:9000",
                    "access_key": "access",
                    "secret_key": "secret",
                    "region": "eu-west-3",
                    "bucket": "raw",
                },
                "postgres": {
                    "database": "vision",
                    "host": "postgres:6432",
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
        }
    )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [("1", 1), ("4000", 4000), ("65535", 65535)],
)
def test_valid_grpc_port_accepts_tcp_range(
    raw_value: str,
    expected: int,
) -> None:
    """Accepts all valid non-reserved TCP port bounds."""
    assert _valid_grpc_port(raw_value) == expected


@pytest.mark.parametrize("raw_value", ["invalid", "0", "-1", "65536"])
def test_valid_grpc_port_rejects_invalid_values(raw_value: str) -> None:
    """Rejects malformed and out-of-range gRPC ports at argument parsing."""
    with pytest.raises(argparse.ArgumentTypeError):
        _valid_grpc_port(raw_value)


def test_build_dagster_grpc_command_targets_exported_definitions() -> None:
    """Pins the module and attribute contract consumed by Dagster workspace YAML."""
    assert _build_dagster_grpc_command("127.0.0.1", 4100) == (
        sys.executable,
        "-m",
        "dagster",
        "api",
        "grpc",
        "-m",
        "galadril_vision.pipeline.defs",
        "-a",
        "defs",
        "--host",
        "127.0.0.1",
        "--port",
        "4100",
    )


@pytest.mark.asyncio
async def test_terminate_code_server_stops_gracefully() -> None:
    """Waits for a responsive Dagster subprocess without forcing a kill."""
    process = MagicMock()
    process.returncode = None
    process.wait = AsyncMock(return_value=0)

    await _terminate_code_server(process)

    process.terminate.assert_called_once_with()
    process.wait.assert_awaited_once_with()
    process.kill.assert_not_called()


@pytest.mark.asyncio
async def test_terminate_code_server_kills_after_timeout() -> None:
    """Escalates termination when the Dagster subprocess does not drain."""
    process = MagicMock()
    process.returncode = None
    process.wait = AsyncMock(return_value=9)

    async def raise_timeout(
        awaitable: Awaitable[int],
        *,
        timeout: float,
    ) -> int:
        assert timeout == 10.0
        cast(Coroutine[object, object, int], awaitable).close()
        raise TimeoutError

    with patch(
        "galadril_vision.main.asyncio.wait_for",
        side_effect=raise_timeout,
    ):
        await _terminate_code_server(process)

    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2


@pytest.mark.asyncio
async def test_main_runs_and_drains_dagster_code_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers gRPC startup, signal handling, and dependency cleanup."""
    config = _vision_config()
    process = MagicMock()
    process.pid = 4242
    process.returncode = None
    fake_loop = MagicMock()

    async def wait_for_shutdown() -> int:
        callback = cast(
            Callable[[], None],
            fake_loop.add_signal_handler.call_args_list[0].args[1],
        )
        callback()
        process.returncode = 0
        return 0

    process.wait = AsyncMock(side_effect=wait_for_shutdown)
    producer = MagicMock()
    producer.flush = AsyncMock()
    pg_client = MagicMock()
    pg_client.connect = AsyncMock()
    pg_client.close = AsyncMock()
    create_subprocess = AsyncMock(return_value=process)
    ensure_topics = AsyncMock()
    run_outbox = AsyncMock()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "galadril-vision",
            "--bootstrap-config",
            "/deployment/pipeline.yaml",
            "--grpc-host",
            "127.0.0.1",
            "--grpc-port",
            "4100",
        ],
    )

    with (
        patch.object(
            VisionConfig,
            "from_yaml",
            return_value=config,
        ) as from_yaml,
        patch("galadril_vision.main.configure_runtime") as configure_runtime,
        patch(
            "galadril_vision.main.ensure_topics",
            ensure_topics,
        ),
        patch(
            "galadril_vision.main.KafkaJsonProducer",
            return_value=producer,
        ),
        patch(
            "galadril_vision.main.PostgresClient",
            return_value=pg_client,
        ),
        patch("galadril_vision.main.AuthzOutboxFlusher"),
        patch(
            "galadril_vision.main._run_authz_outbox_task",
            run_outbox,
        ),
        patch(
            "galadril_vision.main.asyncio.create_subprocess_exec",
            create_subprocess,
        ),
        patch(
            "galadril_vision.main.asyncio.get_running_loop",
            return_value=fake_loop,
        ),
    ):
        await main()

    from_yaml.assert_called_once_with("/deployment/pipeline.yaml")
    configure_runtime.assert_called_once_with(config)
    create_subprocess.assert_awaited_once_with(
        *_build_dagster_grpc_command("127.0.0.1", 4100)
    )
    assert {
        call.args[0] for call in fake_loop.add_signal_handler.call_args_list
    } == {signal.SIGINT, signal.SIGTERM}
    process.terminate.assert_called_once_with()
    pg_client.connect.assert_awaited_once_with()
    pg_client.close.assert_awaited_once_with()
    producer.flush.assert_awaited_once_with(5.0)
