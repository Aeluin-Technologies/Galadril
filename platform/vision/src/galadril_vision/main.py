"""FastStream service entry point for role-specific pipeline workers."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Sequence

import structlog
from faststream import FastStream
from galadril_registry_api import RegistryClient

from galadril_vision.common.config import VisionConfig
from galadril_vision.common.pipelines import (
    _load_artifact,
    wait_for_published_pipeline,
)
from galadril_vision.runtime import configure_runtime, prepare_database
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


def discovery_tenants(value: str) -> tuple[str, ...]:
    """Discovery is bounded to operator-assigned tenants, never inferred from messages."""
    tenants = tuple(
        dict.fromkeys(item.strip() for item in value.split(",") if item.strip())
    )
    if (
        not tenants
        or len(tenants) > 100
        or any(
            re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant) is None
            for tenant in tenants
        )
    ):
        raise ValueError(
            "Discovery requires 1 to 100 explicit tenant identities"
        )
    return tenants


async def serve_publications(
    config: VisionConfig, role: ServiceRole, tenants: tuple[str, ...]
) -> None:
    """Reload a shared consumer only when immutable deployed revisions change."""
    client = RegistryClient(config.registry)
    task: asyncio.Task[None] | None = None
    app: FastStream | None = None
    current: tuple[tuple[str, str, str], ...] = ()
    try:
        while True:
            try:
                found = [
                    (tenant, artifact)
                    for tenant in tenants
                    for artifact in await client.list_published_pipelines(
                        tenant
                    )
                ]
            except Exception as error:
                logger.warning(
                    "pipeline.discovery.unavailable",
                    error_type=type(error).__name__,
                )
                await asyncio.sleep(5)
                continue
            identities = tuple(
                sorted(
                    (tenant, artifact.pipeline_id, artifact.revision_id)
                    for tenant, artifact in found
                )
            )
            if identities != current or task is not None and task.done():
                if task is not None and task.done() and not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        logger.error(
                            "pipeline.runtime.failed",
                            error_type=type(error).__name__,
                        )
                pipelines = tuple(
                    _load_artifact(config, tenant, artifact)
                    for tenant, artifact in found
                )
                if task is not None:
                    if app is not None:
                        await app.stop()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    task = None
                if pipelines:
                    app = build_stream_app(
                        config, role=role, pipelines=pipelines
                    )
                    task = asyncio.create_task(app.run())
                current = identities
                logger.info(
                    "pipeline.discovery.updated",
                    pipelines=len(pipelines),
                    tenants=len(tenants),
                )
            await asyncio.sleep(5)
    finally:
        if task is not None:
            if app is not None:
                await app.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


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
        default=os.getenv(
            "VISION_BOOTSTRAP_PATH", "/etc/galadril/connectors.yaml"
        ),
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
    parser.add_argument(
        "--prepare-database-only",
        action="store_true",
        help="Provision operational tables and exit without loading a pipeline.",
    )
    parser.add_argument(
        "--discover-tenants",
        default=os.getenv("VISION_TENANT_IDS"),
        help="Operator-assigned tenants whose deployed pipelines are watched.",
    )
    parser.add_argument(
        "--tenant-id",
        default=os.getenv("VISION_TENANT_ID"),
        help="Tenant owning the Registry pipeline loaded by this worker.",
    )
    parser.add_argument(
        "--pipeline-id",
        default=os.getenv("VISION_PIPELINE_ID"),
        help="Published Registry pipeline loaded by this worker.",
    )
    parser.add_argument(
        "--revision-id",
        default=os.getenv("VISION_PIPELINE_REVISION"),
        help="Optional immutable Registry revision.",
    )
    args = parser.parse_args(argv)
    if args.prepare_database_only:
        config = await asyncio.to_thread(
            VisionConfig.from_yaml, args.bootstrap_config
        )
        configure_runtime(config, service_name="galadril-vision")
        await prepare_database(config)
        return
    if args.pipeline_config:
        config = await asyncio.to_thread(
            VisionConfig.from_yaml, args.bootstrap_config, args.pipeline_config
        )
        pipelines = None
    else:
        config = await asyncio.to_thread(
            VisionConfig.from_yaml, args.bootstrap_config
        )
        if args.discover_tenants:
            tenants = discovery_tenants(args.discover_tenants)
            configure_runtime(config, service_name="galadril-vision")
            await prepare_database(config)
            await serve_publications(config, args.role, tenants)
            return
        if args.tenant_id is None or args.pipeline_id is None:
            parser.error(
                "--tenant-id and --pipeline-id are required without --pipeline-config"
            )
        configure_runtime(config, service_name="galadril-vision")
        await prepare_database(config)
        pipelines = (
            await wait_for_published_pipeline(
                config,
                args.tenant_id,
                args.pipeline_id,
                args.revision_id,
            ),
        )
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
