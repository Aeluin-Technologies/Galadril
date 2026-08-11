"""Tests for the role-specific FastStream service entry point."""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.common.config import VisionConfig
from galadril_vision.main import _valid_port, create_app, main
from galadril_vision.streaming.app import ServiceRole


def _vision_config() -> VisionConfig:
    """Builds a minimal validated service configuration."""
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
    [("1", 1), ("8000", 8000), ("65535", 65535)],
)
def test_valid_port_accepts_tcp_range(raw_value: str, expected: int) -> None:
    """Accepts valid FastStream ASGI port bounds."""
    assert _valid_port(raw_value) == expected


@pytest.mark.parametrize("raw_value", ["invalid", "0", "-1", "65536"])
def test_valid_port_rejects_invalid_values(raw_value: str) -> None:
    """Rejects malformed and out-of-range ASGI ports."""
    with pytest.raises(argparse.ArgumentTypeError):
        _valid_port(raw_value)


def test_create_app_configures_runtime_and_role() -> None:
    """Wires validated config into the selected FastStream role."""
    config = _vision_config()
    expected_app = MagicMock()
    with (
        patch.object(VisionConfig, "from_yaml", return_value=config) as load,
        patch("galadril_vision.main.configure_runtime") as runtime,
        patch(
            "galadril_vision.main.build_stream_app",
            return_value=expected_app,
        ) as build,
    ):
        app = create_app("/deployment/pipeline.yaml", ServiceRole.GPU)

    assert app is expected_app
    load.assert_called_once_with("/deployment/pipeline.yaml")
    runtime.assert_called_once_with(config, service_name="vision-test-gpu")
    build.assert_called_once_with(config, role=ServiceRole.GPU)


@pytest.mark.asyncio
async def test_main_runs_faststream_asgi_options() -> None:
    """Passes CLI role and listen settings to FastStream."""
    app = MagicMock()
    app.run = AsyncMock()
    with patch("galadril_vision.main.create_app", return_value=app) as factory:
        await main(
            [
                "--bootstrap-config",
                "/deployment/pipeline.yaml",
                "--role",
                "cpu",
                "--host",
                "127.0.0.1",
                "--port",
                "8100",
            ]
        )

    factory.assert_called_once_with(
        "/deployment/pipeline.yaml", ServiceRole.CPU
    )
    app.run.assert_awaited_once_with(
        run_extra_options={"host": "127.0.0.1", "port": 8100}
    )
