"""FastStream service entry point for role-specific pipeline workers."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

import structlog
from faststream import FastStream

from galadril_vision.common.config import VisionConfig
from galadril_vision.common.pipelines import load_published_pipelines
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
    pipeline_path: str | None = None,
) -> FastStream:
    """Loads validated settings and creates the instrumented FastStream app."""
    config = (
        VisionConfig.from_yaml(config_path, pipeline_path)
        if pipeline_path
        else VisionConfig.from_yaml(config_path)
    )
    configure_runtime(config, service_name="galadril-vision")
    return build_stream_app(config, role=role)


async def main(argv: Sequence[str] | None = None) -> None:
    """Runs the role-specific FastStream broker lifecycle."""
    parser = argparse.ArgumentParser(
        description="Run a Galadril FastStream pipeline service."
    )
    parser.add_argument(
        "--bootstrap-config",
        default=os.getenv("VISION_BOOTSTRAP_PATH", "examples/connectors.yaml"),
        help="Path to trusted service connector settings.",
    )
    parser.add_argument(
        "--role",
        type=ServiceRole,
        choices=tuple(ServiceRole),
        default=ServiceRole(os.getenv("PIPELINE_ROLE", ServiceRole.ALL.value)),
        help="Role to run: ingress, cpu, gpu, causal, or all.",
    )
    parser.add_argument(
        "--pipeline-config",
        help="Explicit local example DAG; not tenant database discovery.",
    )
    args = parser.parse_args(argv)
    if args.pipeline_config:
        config = await asyncio.to_thread(
            VisionConfig.from_yaml, args.bootstrap_config, args.pipeline_config
        )
        pipelines = None
    else:
        config = await asyncio.to_thread(
            VisionConfig.from_yaml, args.bootstrap_config
        )
        pipelines = await load_published_pipelines(config)
    configure_runtime(config, service_name="galadril-vision")
    app = (
        build_stream_app(config, role=args.role)
        if pipelines is None
        else build_stream_app(config, role=args.role, pipelines=pipelines)
    )
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
