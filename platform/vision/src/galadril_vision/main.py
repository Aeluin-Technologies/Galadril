"""Entry point for galadril-vision dynamically orchestrated by galadril-pipeline."""

import asyncio
import os
import signal
import sys
import structlog
import argparse
import daft

from galadril_pipeline import PipelineParser

from galadril_vision.common.config import VisionConfig
from galadril_vision.pipeline.runner import VisionPipeline


async def main() -> None:
    logger = structlog.get_logger("main")

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
    config_path = args.config

    try:
        pipeline_graph = PipelineParser.from_yaml(config_path)
    except Exception as exc:
        logger.error("pipeline_load_failed", error=str(exc))
        sys.exit(1)

    yaml_cfg = pipeline_graph.config
    config = VisionConfig()

    if yaml_cfg.galadril_vision.connectorskafka:
        config.kafka.bootstrap_servers = ",".join(
            yaml_cfg.galadril_vision.connectorskafka.brokers
        )
        config.kafka.schema_registry = (
            yaml_cfg.galadril_vision.connectorskafka.schema_registry
        )
        config.kafka.group_id = (
            yaml_cfg.galadril_vision.connectorskafka.consumer_group
        )

    if yaml_cfg.galadril_vision.connectorss3:
        config.image_store.endpoint_url = (
            yaml_cfg.galadril_vision.connectorss3.endpoint
        )
        config.inference.endpoint_url = (
            yaml_cfg.galadril_vision.connectorss3.endpoint
        )

    if yaml_cfg.galadril_vision.connectorspostgres:
        pg = yaml_cfg.galadril_vision.connectorspostgres
        config.postgres.dsn = (
            f"postgresql://{pg.user}:{pg.password}@{pg.host}/{pg.database}"
        )

    logger.info("config_loaded", config=config.model_dump(mode="json"))

    if config.ray.address:
        logger.info("configuring_daft_ray_runner", address=config.ray.address)
        daft.context.set_runner_ray(address=config.ray.address)

    async with VisionPipeline(config, pipeline_graph) as pipeline:
        logger.info("pipeline_started")
        stop_event = asyncio.Event()

        def shutdown_handler(*_) -> None:
            logger.warning("shutdown_signal_received")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown_handler)

        pipeline_task = asyncio.create_task(pipeline.run())

        await stop_event.wait()
        pipeline_task.cancel()
        try:
            await pipeline_task
        except asyncio.CancelledError:
            logger.info("pipeline_cancelled")

    logger.info("pipeline_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        structlog.get_logger("main").error("fatal_error", error=str(exc))
        sys.exit(1)
