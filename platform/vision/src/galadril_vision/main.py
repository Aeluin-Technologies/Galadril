"""FastStream service entry point for role-specific pipeline workers."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import structlog
from faststream import FastStream

from galadril_vision.common.config import VisionConfig
from galadril_vision.runtime import configure_runtime
from galadril_vision.streaming.app import (
    ServiceRole,
    build_stream_app,
)
from galadril_vision.telemetry.logging import configure_logging
from galadril_vision.telemetry.tracing import (
    configure_telemetry,
    shutdown_telemetry,
)

logger = structlog.get_logger("main")


def create_app(
    config_path: str,
    role: ServiceRole,
) -> FastStream:
    """Loads validated settings and creates the instrumented FastStream app."""
    config = VisionConfig.from_yaml(config_path)
    configure_runtime(config, service_name="galadril-vision")
    return build_stream_app(config, role=role)


async def main(argv: Sequence[str] | None = None) -> None:
    """Runs the role-specific FastStream broker lifecycle."""
    parser = argparse.ArgumentParser(
        description="Run a Galadril FastStream pipeline service."
    )
    parser.add_argument(
        "--bootstrap-config",
        default=os.getenv("PIPELINE_PATH", "examples/pipeline.yaml"),
        help="Path to the validated pipeline configuration file.",
    )
    parser.add_argument(
        "--role",
        type=ServiceRole,
        choices=tuple(ServiceRole),
        default=ServiceRole(os.getenv("PIPELINE_ROLE", ServiceRole.ALL.value)),
        help="Role to run: ingress, cpu, gpu, causal, or all.",
    )
    args = parser.parse_args(argv)
    app = create_app(args.bootstrap_config, args.role)
    await app.run()


if __name__ == "__main__":
    _tracer_provider, _meter_provider, logger_provider = configure_telemetry(
        service_name="galadril-vision",
        environment=os.getenv("DEPLOYMENT_ENVIRONMENT", "development"),
        version=os.getenv("OTEL_SERVICE_VERSION", "0.1.0"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        otlp_insecure=os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true").lower()
        == "true",
    )
    configure_logging(
        default_level=os.getenv("LOG_LEVEL", "INFO"),
        enable_json_format=True,
        otlp_logger_provider=logger_provider,
    )
    try:
        import uvloop

        uvloop.run(main())
    except Exception as error:
        logger.error("fatal_error", error=str(error))
        sys.exit(1)
    finally:
        shutdown_telemetry()
