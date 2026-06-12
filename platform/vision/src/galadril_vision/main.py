"""Entry point for galadril-vision."""

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
from galadril_vision.connectors.postgres.graph import GraphStore
from galadril_vision.connectors.postgres.vector import VectorStore
from galadril_vision.pipeline.executor import ESKGPipelineExecutor
from galadril_vision.pipeline.runner import VisionPipeline

logger = structlog.get_logger("main")


async def _run_authz_outbox_task(
    *,
    pg_client: PostgresClient,
    flusher: AuthzOutboxFlusher,
    stop_event: asyncio.Event,
) -> None:
    """Executes the authorization outbox streaming database process worker loop."""
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
        description="Run the Galadril Vision pipeline."
    )
    parser.add_argument(
        "--config",
        type=str,
        default=os.getenv("PIPELINE_PATH", "pipeline.yaml"),
        help="Path to the pipeline configuration YAML file.",
    )
    args = parser.parse_args()

    try:
        cfg = VisionConfig.from_yaml(args.config)
    except Exception as exc:
        logger.error("pipeline_load_failed", error=str(exc))
        sys.exit(1)

    logger.info("pipeline_loaded", name=cfg.name)

    os.environ["AWS_ACCESS_KEY_ID"] = cfg.connectors.s3.access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = cfg.connectors.s3.secret_key
    os.environ["AWS_DEFAULT_REGION"] = cfg.connectors.s3.region
    os.environ["AWS_REGION"] = cfg.connectors.s3.region

    if cfg.ray.address:
        logger.info("configuring_daft_ray_runner", address=cfg.ray.address)
        daft.set_runner_ray(address=cfg.ray.address, noop_if_initialized=True)

    dlq_topic = resolve_authz_dlq_topic(cfg.kafka)
    ensure_topics(
        bootstrap_servers=cfg.kafka.bootstrap_servers,
        topics=[
            KafkaTopicSpec(name=dlq_topic, partitions=1, replication_factor=1)
        ],
    )

    dlq_producer = KafkaJsonProducer(cfg.kafka)

    pg_client = PostgresClient(cfg.postgres)
    await pg_client.connect()

    vector_store = VectorStore(pg_client, cfg.postgres)
    graph_store = GraphStore(pg_client, cfg.postgres)
    await vector_store.initialize()
    await graph_store.initialize()

    executor = ESKGPipelineExecutor(
        config=cfg.to_pipeline_config(),
        vision_config=cfg,
        vector_store=vector_store,
        graph_store=graph_store,
        pg_client=pg_client,
    )

    topics = cfg.get_kafka_topics()
    consumer = KafkaMultiTopicConsumer(
        kafka_cfg=cfg.kafka,
        topics=topics,
        schema_registry_url=cfg.kafka.schema_registry,
        sources=cfg.sources,
    )
    consumer.connect()

    authz_stop = asyncio.Event()

    env = os.getenv("APP_ENV", "production")
    norm_strategy = "tenant" if env == "development" else None
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=cfg.spicedb,
        kafka_cfg=cfg.kafka,
        dlq_producer=dlq_producer,
        subject_normalization_type=norm_strategy,
    )
    authz_task = asyncio.create_task(
        _run_authz_outbox_task(
            pg_client=pg_client, flusher=flusher, stop_event=authz_stop
        )
    )

    pipeline_stop = asyncio.Event()
    pipeline = VisionPipeline(consumer=consumer, executor=executor)
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

    authz_drain_deadline_s = 10.0
    logger.info(
        "shutdown_draining_authz_outbox", seconds=authz_drain_deadline_s
    )

    authz_stop.set()

    try:
        await asyncio.wait_for(authz_task, timeout=authz_drain_deadline_s)
        logger.info("authz_outbox_drain_completed_cleanly")
    except asyncio.TimeoutError:
        logger.warning("authz_outbox_drain_timeout_forced_stop")
    except Exception as exc:
        logger.error("authz_outbox_task_failed_during_drain", error=str(exc))

    consumer.close()
    await pg_client.close()

    try:
        dlq_producer.flush(5.0)
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
