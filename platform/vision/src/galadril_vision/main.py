"""Entry point for multi-tenant galadril-vision pipelines."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from urllib.parse import urlparse

import daft
import structlog

from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.authz.outbox import AuthzOutboxFlusher
from galadril_vision.connectors.kafka.consumer import KafkaMultiTopicConsumer
from galadril_vision.connectors.kafka.producer import (
    KafkaJsonProducer,
    KafkaTopicSpec,
    ensure_topics,
    resolve_authz_dlq_topic,
)
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.common.config import PostgresConnectorConfig
from galadril_vision.connectors.s3.client import S3Client
from galadril_vision.connectors.s3.transit import S3TransitService
from galadril_vision.pipeline.router import MultiTenantPipelineRouter
from galadril_vision.pipeline.runner import VisionPipeline
from galadril_vision.telemetry.logging import configure_logging
from galadril_vision.telemetry.tracing import (
    configure_telemetry,
    shutdown_telemetry,
)

logger = structlog.get_logger("main")


def parse_postgres_host_port(raw_host: str) -> tuple[str, int]:
    """Parses host and port securely, supporting IPv6 and standard formats.

    Shared structural logic mirrored across standalone and Dagster resource contexts.
    """
    if "://" not in raw_host:
        parsed = urlparse(f"tcp://{raw_host}")
    else:
        parsed = urlparse(raw_host)

    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    return host, port


async def _run_authz_outbox_task(
    *,
    pg_client: PostgresClient,
    flusher: AuthzOutboxFlusher,
    stop_event: asyncio.Event,
) -> None:
    try:
        async with pg_client.connection() as conn:
            await flusher.run_forever(
                conn=conn,
                poll_interval_s=0.5,
                batch_size=50,
                stop_event=stop_event,
            )
    except Exception as exc:
        logger.error("authz_outbox_task_failed", error=str(exc))


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Galadril Vision engine."
    )
    parser.add_argument(
        "--bootstrap-config",
        type=str,
        default=os.getenv("PIPELINE_PATH", "bootstrap.yaml"),
        help="Path to the core orchestrator configuration layout file.",
    )
    args = parser.parse_args()

    try:
        base_cfg = VisionConfig.from_yaml(args.bootstrap_config)
    except Exception as exc:
        logger.error("bootstrap_config_load_failed", error=str(exc))
        sys.exit(1)

    env = os.getenv("APP_ENV", "production")
    log_level = os.getenv("LOG_LEVEL", "INFO")
    otlp_logger_provider = None
    if base_cfg.telemetry.enabled:
        _, _, otlp_logger_provider = configure_telemetry(
            service_name=base_cfg.name,
            environment=base_cfg.telemetry.environment,
            version=base_cfg.telemetry.version,
            otlp_endpoint=base_cfg.telemetry.otlp_endpoint,
        )

    configure_logging(
        default_level=log_level,
        enable_json_format=(env != "development"),
        otlp_logger_provider=otlp_logger_provider,
    )
    logger.info("bootstrap_orchestrator_context_loaded", system=base_cfg.name)

    os.environ["AWS_ACCESS_KEY_ID"] = base_cfg.connectors.s3.access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = base_cfg.connectors.s3.secret_key
    os.environ["AWS_DEFAULT_REGION"] = base_cfg.connectors.s3.region
    os.environ["AWS_REGION"] = base_cfg.connectors.s3.region
    os.environ["VISION_STAGING_BUCKET"] = base_cfg.connectors.s3.staging_bucket

    if base_cfg.ray.address:
        logger.info("configuring_daft_ray_runner", address=base_cfg.ray.address)
        daft.set_runner_ray(
            address=base_cfg.ray.address, noop_if_initialized=True
        )

    dlq_topic = resolve_authz_dlq_topic(base_cfg.kafka)
    await ensure_topics(
        bootstrap_servers=base_cfg.kafka.bootstrap_servers,
        topics=[
            KafkaTopicSpec(name=dlq_topic, partitions=1, replication_factor=1)
        ],
    )

    dlq_producer = KafkaJsonProducer(base_cfg.kafka)

    parsed_host, parsed_port = parse_postgres_host_port(base_cfg.postgres.host)
    pg_config = PostgresConnectorConfig(
        host=f"{parsed_host}:{parsed_port}",
        user=base_cfg.postgres.user,
        password=base_cfg.postgres.password,
        database=base_cfg.postgres.database,
        min_connections=base_cfg.postgres.min_connections,
        max_connections=base_cfg.postgres.max_connections,
    )
    master_pg_client = PostgresClient(config=pg_config)
    await master_pg_client.connect()

    raw_s3_client = S3Client(
        bucket=base_cfg.connectors.s3.config_bucket,
        endpoint_url=base_cfg.connectors.s3.endpoint,
        aws_access_key=base_cfg.connectors.s3.access_key,
        aws_secret_key=base_cfg.connectors.s3.secret_key,
        aws_region=base_cfg.connectors.s3.region,
    )
    await raw_s3_client.connect()

    transit_service = S3TransitService(s3_client=raw_s3_client)

    topics = base_cfg.get_kafka_topics() or ["raw"]
    consumer = KafkaMultiTopicConsumer(
        kafka_cfg=base_cfg.kafka,
        topics=topics,
        schema_registry_url=base_cfg.kafka.schema_registry,
        sources=getattr(base_cfg, "sources", []),
    )
    await consumer.connect()

    authz_stop = asyncio.Event()
    norm_strategy = "tenant" if env == "development" else None
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=base_cfg.spicedb,
        kafka_cfg=base_cfg.kafka,
        dlq_producer=dlq_producer,
        subject_normalization_type=norm_strategy,
    )
    authz_task = asyncio.create_task(
        _run_authz_outbox_task(
            pg_client=master_pg_client, flusher=flusher, stop_event=authz_stop
        )
    )

    pipeline_router = MultiTenantPipelineRouter(
        config_bucket=base_cfg.connectors.s3.config_bucket,
        s3_endpoint_url=base_cfg.connectors.s3.endpoint,
        aws_access_key=base_cfg.connectors.s3.access_key,
        aws_secret_key=base_cfg.connectors.s3.secret_key,
        aws_region=base_cfg.connectors.s3.region,
    )

    pipeline_stop = asyncio.Event()
    pipeline = VisionPipeline(
        consumer=consumer,
        transit_service=transit_service,
        pipeline_router=pipeline_router,
        global_batch_timeout_s=getattr(base_cfg, "batch_timeout_s", 60.0)
        or 60.0,
        dlq_producer=dlq_producer,
        dlq_topic=dlq_topic,
    )
    pipeline_task = asyncio.create_task(pipeline.run(stop_event=pipeline_stop))

    shutdown_requested = False

    def request_shutdown(*_) -> None:
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            logger.warning("shutdown_requested_graceful")
            pipeline_stop.set()
        else:
            logger.error("shutdown_requested_forced_immediate_exit")
            sys.exit(1)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_shutdown)

    try:
        await pipeline_task
    except asyncio.CancelledError:
        logger.warning("pipeline_task_cancelled")
    except Exception as exc:
        logger.error("pipeline_task_failed", error=str(exc))

    logger.info("shutdown_draining_authz_outbox")
    authz_stop.set()

    try:
        await asyncio.wait_for(authz_task, timeout=10.0)
    except Exception as exc:
        logger.error("authz_outbox_task_failed_during_drain", error=str(exc))

    await consumer.close()
    await raw_s3_client.close()
    await master_pg_client.close()

    try:
        await dlq_producer.flush(5.0)
    except Exception as exc:
        logger.warning("dlq_producer_flush_failed", error=str(exc))

    logger.info("shutdown_complete")


if __name__ == "__main__":
    initial_level = os.getenv("LOG_LEVEL", "INFO")
    initial_env = os.getenv("APP_ENV", "production")
    configure_logging(
        default_level=initial_level,
        enable_json_format=(initial_env != "development"),
    )

    try:
        asyncio.run(main())
    except Exception as exc:
        structlog.get_logger("main").error("fatal_error", error=str(exc))
        sys.exit(1)
    finally:
        shutdown_telemetry()
