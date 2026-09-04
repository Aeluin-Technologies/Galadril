"""Tests for shared galadril-vision process initialization."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from galadril_vision.common.config import VisionConfig
from galadril_vision.runtime import configure_runtime


def _vision_config(*, telemetry_enabled: bool) -> VisionConfig:
    """Builds configuration for both service and gRPC worker processes."""
    return VisionConfig.model_validate(
        {
            "name": "vision-runtime-test",
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
                    "staging_bucket": "staging",
                },
                "postgres": {
                    "database": "vision",
                    "host": "postgres:5432",
                    "user": "vision",
                    "password": "secret",
                },
                "spicedb": {
                    "endpoint": "spicedb:50051",
                    "token": "token",
                },
                "telemetry": {
                    "enabled": telemetry_enabled,
                    "otlp_endpoint": "http://otel:4317",
                    "environment": "test",
                    "version": "2.0.0",
                },
            },
        }
    )


def test_configure_runtime_sets_worker_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagates validated storage settings without initializing telemetry."""
    config = _vision_config(telemetry_enabled=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    with (
        patch("galadril_vision.runtime.configure_telemetry") as telemetry,
        patch("galadril_vision.runtime.configure_logging") as logging,
    ):
        configure_runtime(config, service_name="vision-runtime-test-cpu")

    telemetry.assert_not_called()
    logging.assert_called_once_with(
        default_level="DEBUG",
        enable_json_format=True,
        otlp_logger_provider=None,
    )
    assert os.environ["AWS_ACCESS_KEY_ID"] == "access"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "secret"
    assert os.environ["AWS_DEFAULT_REGION"] == "eu-west-3"
    assert os.environ["AWS_REGION"] == "eu-west-3"
    assert os.environ["VISION_STAGING_BUCKET"] == "staging"


def test_configure_runtime_wires_otlp_logging_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shares the telemetry logger provider with structured logging."""
    config = _vision_config(telemetry_enabled=True)
    provider = MagicMock()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    with (
        patch(
            "galadril_vision.runtime.configure_telemetry",
            return_value=(MagicMock(), MagicMock(), provider),
        ) as telemetry,
        patch("galadril_vision.runtime.configure_logging") as logging,
    ):
        configure_runtime(config, service_name="vision-runtime-test-cpu")

    telemetry.assert_called_once_with(
        service_name="vision-runtime-test-cpu",
        environment="test",
        version="2.0.0",
        otlp_endpoint="http://otel:4317",
        otlp_insecure=False,
    )
    logging.assert_called_once_with(
        default_level="INFO",
        enable_json_format=True,
        otlp_logger_provider=provider,
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
