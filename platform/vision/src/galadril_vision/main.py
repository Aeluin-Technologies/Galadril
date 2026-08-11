"""FastStream service entry point for role-specific pipeline workers."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import structlog
from faststream.asgi import AsgiFastStream

from galadril_vision.common.config import VisionConfig
from galadril_vision.runtime import configure_runtime
from galadril_vision.streaming.app import (
    ServiceRole,
    build_stream_app,
)
from galadril_vision.telemetry.logging import configure_logging
from galadril_vision.telemetry.tracing import shutdown_telemetry

logger = structlog.get_logger("main")


def _valid_port(value: str) -> int:
    """Validates the FastStream ASGI TCP port."""
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def create_app(
    config_path: str,
    role: ServiceRole,
) -> AsgiFastStream:
    """Loads validated settings and creates the instrumented FastStream app."""
    config = VisionConfig.from_yaml(config_path)
    configure_runtime(config, service_name=f"{config.name}-{role.value}")
    return build_stream_app(config, role=role)


async def main(argv: Sequence[str] | None = None) -> None:
    """Runs the FastStream ASGI gateway with uvicorn-compatible options."""
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
    parser.add_argument(
        "--host",
        default=os.getenv("FASTSTREAM_HOST", "0.0.0.0"),
        help="ASGI listen interface.",
    )
    parser.add_argument(
        "--port",
        type=_valid_port,
        default=_valid_port(os.getenv("FASTSTREAM_PORT", "8000")),
        help="ASGI listen port.",
    )
    args = parser.parse_args(argv)
    app = create_app(args.bootstrap_config, args.role)
    await app.run(run_extra_options={"host": args.host, "port": args.port})


if __name__ == "__main__":
    configure_logging(
        default_level=os.getenv("LOG_LEVEL", "INFO"),
        enable_json_format=os.getenv("APP_ENV", "production") != "development",
    )
    try:
        import uvloop

        uvloop.run(main())
    except Exception as error:
        logger.error("fatal_error", error=str(error))
        sys.exit(1)
    finally:
        shutdown_telemetry()
