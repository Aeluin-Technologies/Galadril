"""Entry point for galadril-vision (composition root)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import daft
import structlog
import daft

from galadril_pipeline import PipelineParser
from galadril_vision.connectors.authz.outbox import AuthzOutboxFlusher
from galadril_vision.common.config import VisionConfig
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
    """Run the authz outbox flusher using a dedicated DB connection."""
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
        pipeline_graph = PipelineParser.from_yaml(args.config)
    except Exception as exc:
        logger.error("pipeline_load_failed", error=str(exc))
        sys.exit(1)

    yaml_cfg = pipeline_graph.config
    cfg = VisionConfig()

    if yaml_cfg.connectors.kafka:
        cfg.kafka.bootstrap_servers = ",".join(
            yaml_cfg.connectors.kafka.brokers
        )
        cfg.kafka.schema_registry = yaml_cfg.connectors.kafka.schema_registry
        cfg.kafka.group_id = yaml_cfg.connectors.kafka.consumer_group

    if yaml_cfg.connectors.s3:
        cfg.image_store.endpoint_url = yaml_cfg.connectors.s3.endpoint
        cfg.inference.endpoint_url = yaml_cfg.connectors.s3.endpoint

    if yaml_cfg.connectors.postgres:
        pg = yaml_cfg.connectors.postgres
        cfg.postgres.dsn = (
            f"postgresql://{pg.user}:{pg.password}@{pg.host}/{pg.database}"
        )

    if getattr(yaml_cfg.connectors, "spicedb", None):
        sp = yaml_cfg.connectors.spicedb
        cfg.spicedb.endpoint = sp.endpoint
        cfg.spicedb.token = sp.token
        cfg.spicedb.schema_name = getattr(sp, "schema_name", None)

    logger.info("config_loaded", config=cfg.model_dump(mode="json"))

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
        config=pipeline_graph.config,
        vision_config=cfg,
        vector_store=vector_store,
        graph_store=graph_store,
        pg_client=pg_client,
    )

    topics = pipeline_graph.get_kafka_topics()
    consumer = KafkaMultiTopicConsumer(
        cfg.kafka,
        topics=topics,
        schema_registry_url=cfg.kafka.schema_registry,
    )
    consumer.connect()

    authz_stop = asyncio.Event()
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=cfg.spicedb,
        kafka_cfg=cfg.kafka,
        dlq_producer=dlq_producer,
    )
    authz_task = asyncio.create_task(
        _run_authz_outbox_task(
            pg_client=pg_client,
            flusher=flusher,
            stop_event=authz_stop,
        )
    )

    pipeline = VisionPipeline(consumer=consumer, executor=executor)

    stop_event: asyncio.Event = asyncio.Event()

    def shutdown_handler(*_) -> None:
        logger.warning("shutdown_signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    pipeline_task = asyncio.create_task(pipeline.run())
    await stop_event.wait()

    logger.info("shutdown_started")
    pipeline_task.cancel()
    try:
        await pipeline_task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("pipeline_task_join_failed", error=str(exc))

    authz_stop.set()
    try:
        await asyncio.wait_for(authz_task, timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("authz_task_timeout")
    except Exception as exc:
        logger.warning("authz_task_join_failed", error=str(exc))

    consumer.close()
    await pg_client.close()

    try:
        dlq_producer.flush(5.0)
    except Exception as exc:
        logger.warning("dlq_producer_flush_failed", error=str(exc))

    logger.info("shutdown_complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        structlog.get_logger("main").error("fatal_error", error=str(exc))
        sys.exit(1)
