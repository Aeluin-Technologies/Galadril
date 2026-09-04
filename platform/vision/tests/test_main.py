"""Tests for the role-specific FastStream service entry point."""

from __future__ import annotations

import asyncio
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
        app = create_app("/deployment/connectors.yaml", ServiceRole.GPU)

    assert app is expected_app
    load.assert_called_once_with("/deployment/connectors.yaml")
    runtime.assert_called_once_with(config, service_name="galadril-vision")
    build.assert_called_once_with(config, role=ServiceRole.GPU)


def test_main_runs_faststream_broker_lifecycle() -> None:
    """Starts FastStream directly without an embedded HTTP server."""
    app = MagicMock()
    app.run = AsyncMock()
    config = _vision_config()
    with (
        patch.object(VisionConfig, "from_yaml", return_value=config) as load,
        patch("galadril_vision.main.configure_runtime"),
        patch(
            "galadril_vision.main.build_stream_app", return_value=app
        ) as factory,
    ):
        asyncio.run(
            main(
                [
                    "--bootstrap-config",
                    "/deployment/connectors.yaml",
                    "--pipeline-config",
                    "/deployment/pipeline.example.yaml",
                    "--role",
                    "cpu",
                ]
            )
        )

    load.assert_called_once_with(
        "/deployment/connectors.yaml", "/deployment/pipeline.example.yaml"
    )
    factory.assert_called_once_with(config, role=ServiceRole.CPU)
    app.run.assert_awaited_once_with()


def test_main_loads_all_tenant_publications_by_default() -> None:
    """Production startup creates one service over all configured tenants."""
    app = MagicMock()
    app.run = AsyncMock()
    bootstrap = _vision_config()
    published = (_vision_config(), _vision_config())
    published[0].name = "tenant_a/daily/aaaaaaaa"
    published[1].name = "tenant_b/hourly/bbbbbbbb"
    with (
        patch.object(VisionConfig, "from_yaml", return_value=bootstrap),
        patch(
            "galadril_vision.main.load_published_pipelines",
            AsyncMock(return_value=published),
        ) as load,
        patch("galadril_vision.main.configure_runtime"),
        patch(
            "galadril_vision.main.build_stream_app", return_value=app
        ) as factory,
    ):
        asyncio.run(
            main(
                [
                    "--bootstrap-config",
                    "/deployment/connectors.yaml",
                    "--role",
                    "all",
                ]
            )
        )

    load.assert_awaited_once_with(bootstrap)
    factory.assert_called_once_with(
        bootstrap,
        role=ServiceRole.ALL,
        pipelines=published,
    )
    app.run.assert_awaited_once_with()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
