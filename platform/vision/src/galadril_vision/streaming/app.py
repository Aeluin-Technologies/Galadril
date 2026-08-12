"""FastStream application factory for local or shared Ray-backed workers."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import (
    AsyncIterator,
    Sequence,
)
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import cast

import structlog
from faststream import FastStream
from faststream.confluent import KafkaBroker, KafkaMessage
from faststream.confluent.helpers.config import ConfluentConfig
from faststream.confluent.opentelemetry import KafkaTelemetryMiddleware
from faststream.middlewares import AckPolicy
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_pipeline.routing import PipelineRouteTable
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace

from galadril_vision.actors.dispatcher import ActorHandle, RayActorDispatcher
from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.authz.outbox import AuthzOutboxFlusher
from galadril_vision.connectors.kafka.producer import KafkaJsonProducer
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.pipeline.ledger import PostgresExecutionLedger
from galadril_vision.streaming.codec import AvroMessageDecoder
from galadril_vision.streaming.handlers import (
    AvroEnvelope,
    CommandHandler,
    CommandInProgress,
    IngressHandler,
)
from galadril_vision.streaming.timer import (
    CronCommandPublisher,
    ScheduledCommandFactory,
)
from galadril_vision.streaming.topics import TopicLayout
from galadril_vision.telemetry.metrics import PipelineMetrics

logger = structlog.get_logger(__name__)
faststream_logger = logging.getLogger("galadril_vision.faststream")


class ServiceRole(StrEnum):
    """Deployment roles that scale independently on one shared code artifact."""

    ALL = "all"
    INGRESS = "ingress"
    CPU = "cpu"
    GPU = "gpu"
    CAUSAL = "causal"


class _Runtime:
    """Owns mutable process resources while handlers remain statically registered."""

    __slots__ = (
        "command_handler",
        "config",
        "authz_stop",
        "authz_task",
        "metrics",
        "postgres",
        "ray_started",
        "resources",
        "routes",
        "topics",
    )

    def __init__(
        self,
        *,
        config: VisionConfig,
        resources: tuple[ResourceClass, ...],
        routes: PipelineRouteTable,
        topics: TopicLayout,
        metrics: PipelineMetrics,
    ) -> None:
        self.config = config
        self.authz_stop = asyncio.Event()
        self.authz_task: asyncio.Task[None] | None = None
        self.resources = resources
        self.routes = routes
        self.topics = topics
        self.metrics = metrics
        self.postgres: PostgresClient | None = None
        self.ray_started = False
        self.command_handler: CommandHandler | None = None

    async def start(self, broker: KafkaBroker) -> None:
        """Connects Postgres and Ray without blocking the FastStream event loop."""
        if not self.resources:
            return
        self.postgres = PostgresClient(self.config.postgres)
        await self.postgres.connect()
        self.ray_started = await asyncio.to_thread(_initialize_ray, self.config)
        actor_pools = {
            resource: _create_actor_pool(self.config, resource)
            for resource in self.resources
        }
        dispatcher = RayActorDispatcher(actor_pools, self.metrics)
        self.command_handler = CommandHandler(
            routes=self.routes,
            publisher=broker,
            dispatcher=dispatcher,
            ledger=PostgresExecutionLedger(self.postgres),
            topics=self.topics,
            metrics=self.metrics,
        )

    async def start_background(self, broker: KafkaBroker) -> None:
        """Starts the CPU-owned authorization outbox after Kafka is connected."""
        if ResourceClass.CPU not in self.resources or self.postgres is None:
            return
        normalization = (
            "tenant"
            if os.getenv("APP_ENV", "production") == "development"
            else None
        )
        flusher = AuthzOutboxFlusher(
            spicedb_cfg=self.config.spicedb,
            kafka_cfg=self.config.kafka,
            dlq_producer=KafkaJsonProducer(broker),
            subject_normalization_type=normalization,
        )

        async def run_outbox() -> None:
            if self.postgres is None:
                return
            async with self.postgres.connection() as connection:
                await flusher.run_forever(
                    conn=connection,
                    stop_event=self.authz_stop,
                )

        self.authz_task = asyncio.create_task(
            run_outbox(), name="authz-outbox-flusher"
        )

    async def stop_background(self) -> None:
        """Drains the authorization outbox task while Kafka is still available."""
        self.authz_stop.set()
        if self.authz_task is None:
            return
        try:
            await asyncio.wait_for(self.authz_task, timeout=10.0)
        except TimeoutError:
            self.authz_task.cancel()
            try:
                await self.authz_task
            except asyncio.CancelledError:
                pass
        finally:
            self.authz_task = None

    async def close(self) -> None:
        """Flushes Ray and database resources owned by this process."""
        if self.ray_started:
            await asyncio.to_thread(_shutdown_ray)
            self.ray_started = False
        if self.postgres is not None:
            await self.postgres.close()
            self.postgres = None


def build_stream_app(
    config: VisionConfig,
    *,
    role: ServiceRole = ServiceRole.ALL,
    topics: TopicLayout | None = None,
) -> FastStream:
    """Builds the fully instrumented FastStream Kafka/Redpanda application."""
    topic_layout = topics or TopicLayout()
    routes = PipelineRouteTable(config.to_pipeline_config())
    pipeline_metrics = PipelineMetrics()
    resources = _resources_for_role(role)
    runtime = _Runtime(
        config=config,
        resources=resources,
        routes=routes,
        topics=topic_layout,
        metrics=pipeline_metrics,
    )
    confluent_config: ConfluentConfig = {"enable.auto.commit": False}
    broker = KafkaBroker(
        config.kafka.brokers,
        config=confluent_config,
        acks="all",
        enable_idempotence=True,
        middlewares=(
            KafkaTelemetryMiddleware(
                tracer_provider=trace.get_tracer_provider(),
                meter_provider=otel_metrics.get_meter_provider(),
                include_messages_counters=True,
            ),
        ),
        logger=faststream_logger,
    )
    ingress_handler = IngressHandler(
        pipeline=config.name,
        routes=routes,
        publisher=broker,
        topics=topic_layout,
        metrics=pipeline_metrics,
    )

    decoder: AvroMessageDecoder | None = None
    if role in {ServiceRole.ALL, ServiceRole.INGRESS}:
        decoder = AvroMessageDecoder(
            sources=cast(list[object], config.sources),
            schema_registry_url=config.kafka.schema_registry,
        )
        _register_ingress(
            broker,
            config,
            decoder,
            ingress_handler,
        )
    for resource in resources:
        _register_command_worker(broker, config, runtime, resource)

    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        try:
            await runtime.start(broker)
            yield
        finally:
            if decoder is not None:
                await decoder.close()
            await runtime.close()

    app = FastStream(
        broker,
        logger=faststream_logger,
        lifespan=lifespan,
    )

    @app.after_startup
    async def start_background_workers() -> None:
        await runtime.start_background(broker)

    @app.on_shutdown
    async def stop_background_workers() -> None:
        await runtime.stop_background()

    if (
        role in {ServiceRole.ALL, ServiceRole.INGRESS}
        and routes.scheduled_steps
    ):
        timer = CronCommandPublisher(
            factory=ScheduledCommandFactory(
                config.to_pipeline_config(), routes
            ),
            publisher=broker,
            topics=topic_layout,
        )
        timer_stop = asyncio.Event()
        timer_task: asyncio.Task[None] | None = None

        @app.after_startup
        async def start_timer() -> None:
            nonlocal timer_task
            timer_task = asyncio.create_task(
                timer.run(timer_stop), name="pipeline-cron-publisher"
            )

        @app.on_shutdown
        async def stop_timer() -> None:
            timer_stop.set()
            if timer_task is not None:
                await timer_task

    return app


def _register_ingress(
    broker: KafkaBroker,
    config: VisionConfig,
    decoder: AvroMessageDecoder,
    handler: IngressHandler,
) -> None:
    """Registers one-message Avro consumers so every Trace ID has one parent."""
    topics = tuple(config.get_kafka_topics())
    if not topics:
        raise ValueError(
            "At least one Kafka source topic is required for ingress"
        )

    @broker.subscriber(
        *topics,
        group_id=f"{config.kafka.group_id}-ingress",
        auto_offset_reset=config.kafka.auto_offset_reset,
        decoder=decoder,
        batch=False,
        ack_policy=AckPolicy.MANUAL,
        title="Avro source ingress",
    )
    async def consume_source(
        event: AvroEnvelope, message: KafkaMessage
    ) -> None:
        try:
            await handler.handle(event)
        except Exception:
            await message.nack()
            raise
        await message.ack()


def _register_command_worker(
    broker: KafkaBroker,
    config: VisionConfig,
    runtime: _Runtime,
    resource: ResourceClass,
) -> None:
    """Registers a bounded-concurrency worker for exactly one resource topic."""
    topic = runtime.topics.commands_for(resource)

    @broker.subscriber(
        topic,
        group_id=f"{config.kafka.group_id}-{resource.value}",
        auto_offset_reset=config.kafka.auto_offset_reset,
        batch=False,
        ack_policy=AckPolicy.MANUAL,
        title=f"{resource.value.upper()} Ray command worker",
    )
    async def consume_command(
        command: PipelineCommand, message: KafkaMessage
    ) -> None:
        handler = runtime.command_handler
        if handler is None:
            await message.nack()
            raise RuntimeError("Command worker runtime is not initialized")
        if command.resource_class is not resource:
            await message.reject()
            raise ValueError(
                f"Command resource '{command.resource_class}' does not match "
                f"topic resource '{resource}'"
            )
        try:
            await handler.handle(command)
        except CommandInProgress:
            await message.nack()
            return
        except Exception:
            await message.nack()
            raise
        await message.ack()


def _resources_for_role(role: ServiceRole) -> tuple[ResourceClass, ...]:
    """Maps deployment roles to the Ray pools required in that process."""
    if role is ServiceRole.ALL:
        return (ResourceClass.CPU, ResourceClass.GPU, ResourceClass.CAUSAL)
    if role is ServiceRole.CPU:
        return (ResourceClass.CPU,)
    if role is ServiceRole.GPU:
        return (ResourceClass.GPU,)
    if role is ServiceRole.CAUSAL:
        return (ResourceClass.CAUSAL,)
    return ()


def _initialize_ray(config: VisionConfig) -> bool:
    """Connects to shared Ray or starts an embedded single-node runtime."""
    import ray

    if ray.is_initialized():
        return False
    address = _resolve_ray_address(config)
    options: dict[str, object] = {
        "address": address,
        "namespace": config.ray.namespace,
        "ignore_reinit_error": True,
        "log_to_driver": False,
    }
    if address is None:
        options.update(
            num_cpus=config.ray.num_cpus,
            num_gpus=config.ray.num_gpus,
            include_dashboard=False,
        )
    ray.init(**options)
    logger.info(
        "ray_runtime_initialized",
        mode="cluster" if address else "local",
        address=address,
        namespace=config.ray.namespace,
    )
    return True


def _resolve_ray_address(config: VisionConfig) -> str | None:
    """Resolves a validated deployment override before the YAML value."""
    environment_address = os.getenv("RAY_ADDRESS")
    if environment_address is None or not environment_address.strip():
        return config.ray.address
    return type(config.ray)(address=environment_address).address


def _shutdown_ray() -> None:
    """Closes the Ray Client or embedded runtime outside the event loop."""
    import ray

    ray.shutdown()


def _create_actor_pool(
    config: VisionConfig, resource: ResourceClass
) -> Sequence[ActorHandle]:
    """Creates named actors whose process-local dependencies remain warm."""
    from galadril_vision.actors.processor import VisionCommandProcessor
    from galadril_vision.actors.worker import RayPipelineActor

    handles: list[ActorHandle] = []
    telemetry = {
        "enabled": config.telemetry.enabled,
        "service_name": f"{config.name}-ray-{resource.value}",
        "environment": config.telemetry.environment,
        "version": config.telemetry.version,
        "otlp_endpoint": config.telemetry.otlp_endpoint,
        "otlp_insecure": config.telemetry.otlp_insecure,
    }
    for index in range(config.ray.actor_replicas):
        options: dict[str, object] = {
            "name": f"{config.name}-{resource.value}-{index}",
            "namespace": config.ray.namespace,
            "get_if_exists": True,
            "max_concurrency": 1,
            "num_cpus": 1,
        }
        if resource is ResourceClass.GPU:
            gpu_requirement = _gpu_actor_requirement(config)
            if gpu_requirement > 0:
                options["num_gpus"] = gpu_requirement
        handle = RayPipelineActor.options(**options).remote(
            VisionCommandProcessor(config), telemetry
        )
        handles.append(cast(ActorHandle, handle))
    return tuple(handles)


def _gpu_actor_requirement(config: VisionConfig) -> float:
    """Selects GPU scheduling demand without blocking CPU-only local systems."""
    configured = config.ray.gpu_actor_num_gpus
    if configured is not None:
        return configured
    if _resolve_ray_address(config) is not None:
        return 1.0

    import ray

    detected = float(ray.cluster_resources().get("GPU", 0.0))
    return min(1.0, detected)
