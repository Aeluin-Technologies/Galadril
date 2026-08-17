"""Tests for the role-specific FastStream service entry point."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.common.config import VisionConfig
from galadril_vision.main import create_app, main
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
    runtime.assert_called_once_with(config, service_name="galadril-vision")
    build.assert_called_once_with(config, role=ServiceRole.GPU)


@pytest.mark.asyncio
async def test_main_runs_faststream_broker_lifecycle() -> None:
    """Starts FastStream directly without an embedded HTTP server."""
    app = MagicMock()
    app.run = AsyncMock()
    with patch("galadril_vision.main.create_app", return_value=app) as factory:
        await main(
            [
                "--bootstrap-config",
                "/deployment/pipeline.yaml",
                "--role",
                "cpu",
            ]
        )

    factory.assert_called_once_with(
        "/deployment/pipeline.yaml", ServiceRole.CPU
    )
    app.run.assert_awaited_once_with()
