"""Entry point for multi-tenant galadril-vision pipelines."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

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
from galadril_vision.pipeline.runner import VisionPipeline
from galadril_vision.pipeline.router import MultiTenantPipelineRouter

logger = structlog.get_logger("main")


async def _run_authz_outbox_task(
    *,
    pg_client: PostgresClient,
    flusher: AuthzOutboxFlusher,
    stop_event: asyncio.Event,
) -> None:
    """Executes the authorization outbox streaming database process worker loop.

    Args:
        pg_client: Active PostgreSQL connection client.
        flusher: Service managing the streaming logic.
        stop_event: Signal event to cleanly terminate the loop.
    """
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
    """Configures environment settings, mounts internal connectors and boots processing runloops."""
    import argparse

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

    logger.info("bootstrap_orchestrator_context_loaded", system=base_cfg.name)

    os.environ["AWS_ACCESS_KEY_ID"] = base_cfg.connectors.s3.access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = base_cfg.connectors.s3.secret_key
    os.environ["AWS_DEFAULT_REGION"] = base_cfg.connectors.s3.region
    os.environ["AWS_REGION"] = base_cfg.connectors.s3.region

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

    master_pg_client = PostgresClient(base_cfg.postgres)
    await master_pg_client.connect()

    router = MultiTenantPipelineRouter(
        config_bucket=base_cfg.connectors.s3.config_bucket,
        cache_capacity=40,
        s3_endpoint_url=base_cfg.connectors.s3.endpoint,
        aws_access_key=base_cfg.connectors.s3.access_key,
        aws_secret_key=base_cfg.connectors.s3.secret_key,
        aws_region=base_cfg.connectors.s3.region,
    )

    topics = base_cfg.get_kafka_topics()
    if not topics:
        topics = ["raw"]
        logger.info("using_default_shared_intake_topics", topics=topics)

    consumer = KafkaMultiTopicConsumer(
        kafka_cfg=base_cfg.kafka,
        topics=topics,
        schema_registry_url=base_cfg.kafka.schema_registry,
        sources=getattr(base_cfg, "sources", []),
    )
    await consumer.connect()

    authz_stop = asyncio.Event()
    env = os.getenv("APP_ENV", "production")
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

    pipeline_stop = asyncio.Event()
    pipeline = VisionPipeline(
        consumer=consumer,
        router=router,
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
    await master_pg_client.close()
    await router.close()

    try:
        await dlq_producer.flush(5.0)
    except Exception as exc:
        logger.warning("dlq_producer_flush_failed", error=str(exc))

    logger.info("shutdown_complete")


if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.KeyValueRenderer(),
        ]
    )

    try:
        asyncio.run(main())
    except Exception as exc:
        structlog.get_logger("main").error("fatal_error", error=str(exc))
        sys.exit(1)
