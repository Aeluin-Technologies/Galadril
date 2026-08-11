"""Shared runtime initialization for FastStream and Ray processes."""

from __future__ import annotations

import os

import structlog

from galadril_vision.common.config import VisionConfig
from galadril_vision.telemetry.logging import configure_logging
from galadril_vision.telemetry.tracing import configure_telemetry

logger = structlog.get_logger(__name__)


def configure_runtime(
    config: VisionConfig, *, service_name: str | None = None
) -> None:
    """Configures observability and cloud credentials from validated settings."""
    environment = os.getenv("APP_ENV", "production")
    log_level = os.getenv("LOG_LEVEL", "INFO")
    otlp_logger_provider = None
    effective_service_name = service_name or config.name

    if config.telemetry.enabled:
        _, _, otlp_logger_provider = configure_telemetry(
            service_name=effective_service_name,
            environment=config.telemetry.environment,
            version=config.telemetry.version,
            otlp_endpoint=config.telemetry.otlp_endpoint,
            otlp_insecure=config.telemetry.otlp_insecure,
        )

    configure_logging(
        default_level=log_level,
        enable_json_format=(environment != "development"),
        otlp_logger_provider=otlp_logger_provider,
    )

    os.environ["AWS_ACCESS_KEY_ID"] = config.connectors.s3.access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = config.connectors.s3.secret_key
    os.environ["AWS_DEFAULT_REGION"] = config.connectors.s3.region
    os.environ["AWS_REGION"] = config.connectors.s3.region
    os.environ["VISION_STAGING_BUCKET"] = config.connectors.s3.staging_bucket

    logger.info(
        "runtime_context_configured",
        system=config.name,
        service_name=effective_service_name,
    )
