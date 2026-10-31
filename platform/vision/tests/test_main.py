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


def test_main_loads_only_the_requested_tenant_pipeline() -> None:
    """Production startup resolves one explicitly scoped Registry pipeline."""
    app = MagicMock()
    app.run = AsyncMock()
    bootstrap = _vision_config()
    published = _vision_config()
    published.name = "tenant_a/daily/aaaaaaaa"
    with (
        patch.object(VisionConfig, "from_yaml", return_value=bootstrap),
        patch(
            "galadril_vision.main.prepare_database", AsyncMock(), create=True
        ) as prepare,
        patch(
            "galadril_vision.main.wait_for_published_pipeline",
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
                    "--tenant-id",
                    "tenant_a",
                    "--pipeline-id",
                    "daily",
                    "--revision-id",
                    "aaaaaaaaaaaaaaaaaaaa",
                ]
            )
        )

    load.assert_awaited_once_with(
        bootstrap, "tenant_a", "daily", "aaaaaaaaaaaaaaaaaaaa"
    )
    prepare.assert_awaited_once_with(bootstrap)
    factory.assert_called_once_with(
        bootstrap,
        role=ServiceRole.ALL,
        pipelines=(published,),
    )
    app.run.assert_awaited_once_with()


def test_database_preparation_does_not_require_a_published_pipeline() -> None:
    config = _vision_config()
    with (
        patch.object(VisionConfig, "from_yaml", return_value=config),
        patch("galadril_vision.main.configure_runtime"),
        patch("galadril_vision.main.prepare_database", AsyncMock()) as prepare,
        patch(
            "galadril_vision.main.wait_for_published_pipeline", AsyncMock()
        ) as wait,
        patch("galadril_vision.main.build_stream_app") as build,
    ):
        asyncio.run(
            main(
                [
                    "--bootstrap-config",
                    "/connectors.yaml",
                    "--prepare-database-only",
                ]
            )
        )
    prepare.assert_awaited_once_with(config)
    wait.assert_not_awaited()
    build.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))


def test_discovery_requires_explicit_tenant_scope() -> None:
    from galadril_vision.main import discovery_tenants

    assert discovery_tenants(" a, b,a ") == ("a", "b")
    with pytest.raises(ValueError):
        discovery_tenants(" ")
    with pytest.raises(ValueError):
        discovery_tenants("../foreign")


def test_discovery_reloads_changed_publications_and_stops_on_cancellation() -> (
    None
):
    from galadril_registry_api import PipelineArtifact
    from galadril_vision.main import serve_publications

    config = _vision_config()
    artifact = PipelineArtifact("mail", "a" * 32, b"{}")
    client = MagicMock()
    client.list_published_pipelines = AsyncMock(
        side_effect=[(artifact,), (artifact,), ()]
    )
    client.close = AsyncMock()
    app = MagicMock()
    app.stop = AsyncMock()

    async def run() -> None:
        await asyncio.Event().wait()

    app.run = run
    real_sleep = asyncio.sleep
    polls = 0

    async def tick(delay: float) -> None:
        nonlocal polls
        polls += 1
        if polls == 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    with (
        patch("galadril_vision.main.RegistryClient", return_value=client),
        patch("galadril_vision.main._load_artifact", return_value=config),
        patch(
            "galadril_vision.main.build_stream_app", return_value=app
        ) as build,
        patch("galadril_vision.main.asyncio.sleep", side_effect=tick),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(serve_publications(config, ServiceRole.CPU, ("tenant",)))
    build.assert_called_once_with(
        config, role=ServiceRole.CPU, pipelines=(config,)
    )
    app.stop.assert_awaited_once()
    client.close.assert_awaited_once()


def test_graphical_layout_is_accepted_without_changing_service_connectors() -> (
    None
):
    bootstrap = _vision_config()
    config = VisionConfig.with_pipeline(
        bootstrap.model_dump(),
        {
            "name": "mail",
            "sources": [
                {
                    "id": "mail",
                    "account_id": "a" * 32,
                    "topic": "raw",
                    "match_pattern": "email-.*",
                    "schema_path": "email.avsc",
                }
            ],
            "pipeline": [],
            "editor": {"positions": {"mail": {"x": 40, "y": 80}}},
        },
    )
    assert config.name == "mail"
    assert config.to_pipeline_config().sources[0].account_id == "a" * 32
    assert config.connectors == bootstrap.connectors
    assert "editor" not in config.model_dump()
    with pytest.raises(ValueError, match="service settings"):
        VisionConfig.with_pipeline(
            bootstrap.model_dump(),
            {
                "name": "bad",
                "sources": [],
                "pipeline": [],
                "editor": {},
                "connectors": {},
            },
        )
