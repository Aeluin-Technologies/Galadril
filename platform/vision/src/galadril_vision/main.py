"""Entry point for multi-tenant galadril-vision pipelines."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from urllib.parse import urlparse

import structlog

from galadril_vision.common.config import PostgresConnectorConfig, VisionConfig
from galadril_vision.connectors.authz.outbox import AuthzOutboxFlusher
from galadril_vision.connectors.kafka.producer import (
    KafkaJsonProducer,
    KafkaTopicSpec,
    ensure_topics,
    resolve_authz_dlq_topic,
)
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.runtime import configure_runtime
from galadril_vision.telemetry.logging import configure_logging
from galadril_vision.telemetry.tracing import shutdown_telemetry

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


def _valid_grpc_port(value: str) -> int:
    """Validates the TCP port used by the Dagster gRPC code server."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc

    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def _build_dagster_grpc_command(host: str, port: int) -> tuple[str, ...]:
    """Builds the command for the Dagster code server hosting vision definitions."""
    return (
        sys.executable,
        "-m",
        "dagster",
        "api",
        "grpc",
        "-m",
        "galadril_vision.pipeline.defs",
        "-a",
        "defs",
        "--host",
        host,
        "--port",
        str(port),
    )


async def _terminate_code_server(
    code_server: asyncio.subprocess.Process,
) -> None:
    """Terminates the code server and escalates only if its graceful stop times out."""
    if code_server.returncode is not None:
        return

    code_server.terminate()
    try:
        await asyncio.wait_for(code_server.wait(), timeout=10.0)
    except TimeoutError:
        logger.warning("dagster_code_server_force_kill")
        code_server.kill()
        await code_server.wait()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Galadril Vision Dagster code server."
    )
    parser.add_argument(
        "--bootstrap-config",
        type=str,
        default=os.getenv("PIPELINE_PATH", "bootstrap.yaml"),
        help="Path to the validated pipeline configuration file.",
    )
    parser.add_argument(
        "--grpc-host",
        default=os.getenv("DAGSTER_GRPC_HOST", "0.0.0.0"),
        help="Interface exposed by the Dagster gRPC code server.",
    )
    parser.add_argument(
        "--grpc-port",
        type=_valid_grpc_port,
        default=_valid_grpc_port(os.getenv("DAGSTER_GRPC_PORT", "4000")),
        help="TCP port exposed by the Dagster gRPC code server.",
    )
    args = parser.parse_args()

    try:
        base_cfg = VisionConfig.from_yaml(args.bootstrap_config)
    except Exception as exc:
        logger.error("bootstrap_config_load_failed", error=str(exc))
        sys.exit(1)

    os.environ["PIPELINE_PATH"] = args.bootstrap_config
    configure_runtime(base_cfg)

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

    authz_stop = asyncio.Event()
    norm_strategy = (
        "tenant" if os.getenv("APP_ENV", "production") == "development" else None
    )
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

    shutdown_requested = False
    code_server: asyncio.subprocess.Process | None = None

    def request_shutdown(*_) -> None:
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            logger.warning("shutdown_requested_graceful")
            authz_stop.set()
            if code_server is not None and code_server.returncode is None:
                code_server.terminate()
        else:
            logger.error("shutdown_requested_forced_immediate_exit")
            sys.exit(1)

    try:
        code_server = await asyncio.create_subprocess_exec(
            *_build_dagster_grpc_command(args.grpc_host, args.grpc_port)
        )
        logger.info(
            "dagster_code_server_started",
            host=args.grpc_host,
            port=args.grpc_port,
            pid=code_server.pid,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_shutdown)

        exit_code = await code_server.wait()
        if not shutdown_requested:
            raise RuntimeError(
                f"Dagster code server stopped unexpectedly with status {exit_code}."
            )
    finally:
        authz_stop.set()
        if code_server is not None:
            await _terminate_code_server(code_server)

        logger.info("shutdown_draining_authz_outbox")
        try:
            await asyncio.wait_for(authz_task, timeout=10.0)
        except TimeoutError:
            logger.warning("authz_outbox_task_drain_timed_out")
            authz_task.cancel()
            try:
                await authz_task
            except asyncio.CancelledError:
                pass
        except Exception as exc:
            logger.error("authz_outbox_task_failed_during_drain", error=str(exc))

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
