"""FastStream application factory for local or shared Ray-backed workers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace

from galadril_vision.actors.dispatcher import ActorHandle, RayActorDispatcher
from galadril_vision.common.config import VisionConfig
from galadril_vision.common.pipelines import (
    PipelineRuntimeRegistry,
    PipelineUnavailable,
)
from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.connectors.authz.outbox import AuthzOutboxFlusher
from galadril_vision.connectors.kafka.producer import KafkaJsonProducer
from galadril_vision.connectors.kafka.schemas import EventNormalizer
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
        "registry",
        "topics",
    )

    def __init__(
        self,
        *,
        config: VisionConfig,
        resources: tuple[ResourceClass, ...],
        registry: PipelineRuntimeRegistry,
        topics: TopicLayout,
        metrics: PipelineMetrics,
    ) -> None:
        self.config = config
        self.authz_stop = asyncio.Event()
        self.authz_task: asyncio.Task[None] | None = None
        self.resources = resources
        self.registry = registry
        self.topics = topics
        self.metrics = metrics
        self.postgres: PostgresClient | None = None
        self.ray_started = False
        self.command_handler: dict[str, CommandHandler] | None = None

    async def start(self, broker: KafkaBroker) -> None:
        """Connects Postgres and Ray without blocking the FastStream event loop."""
        if not self.resources:
            return
        self.postgres = PostgresClient(self.config.postgres)
        await self.postgres.connect(initialize_database_infrastructure=True)
        self.ray_started = await asyncio.to_thread(_initialize_ray, self.config)
        actor_pools = {
            resource: _create_actor_pool(
                self.config, self.registry.configs, resource
            )
            for resource in self.resources
        }
        dispatcher = RayActorDispatcher(actor_pools, self.metrics)
        ledger = PostgresExecutionLedger(self.postgres)
        self.command_handler = {
            config.name: CommandHandler(
                routes=self.registry.routes_for(config.name),
                publisher=broker,
                dispatcher=dispatcher,
                ledger=ledger,
                topics=self.topics,
                metrics=self.metrics,
            )
            for config in self.registry.configs
        }

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
            async with self.postgres.maintenance_connection() as connection:
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


class _MultiPipelineIngress:
    """Routes one normalized record into its immutable published DAG."""

    __slots__ = ("_handlers", "_registry")

    def __init__(
        self,
        registry: PipelineRuntimeRegistry,
        handlers: dict[str, IngressHandler],
    ) -> None:
        self._registry = registry
        self._handlers = handlers

    async def handle(
        self, envelope: AvroEnvelope
    ) -> tuple[PipelineCommand, ...]:
        started_at = time.perf_counter()
        fallback = next(iter(self._handlers.values()))
        try:
            normalized = EventNormalizer.normalize(
                envelope.payload, envelope.source_id
            )
            record = CanonicalRecord.model_validate(normalized)
        except Exception as error:
            return await fallback.reject(envelope, error, started_at=started_at)
        if record.tenant_id != envelope.tenant_id:
            return await fallback.reject(
                envelope,
                PipelineUnavailable(
                    "Payload tenant does not match its trusted Kafka identity"
                ),
                started_at=started_at,
            )
        source_id = record.source
        try:
            config = self._registry.for_ingress_identity(
                envelope.tenant_id,
                envelope.pipeline_id,
                envelope.revision_id,
                source_id,
            )
        except PipelineUnavailable:
            if source_id == envelope.source_id:
                return await fallback.reject(
                    envelope,
                    PipelineUnavailable(
                        "No published pipeline accepts this immutable source identity"
                    ),
                    started_at=started_at,
                )
            source_id = envelope.source_id
            try:
                config = self._registry.for_ingress_identity(
                    envelope.tenant_id,
                    envelope.pipeline_id,
                    envelope.revision_id,
                    source_id,
                )
            except PipelineUnavailable as error:
                return await fallback.reject(
                    envelope, error, started_at=started_at
                )
        if config.runtime_tenant_id != record.tenant_id:
            return await fallback.reject(
                envelope,
                PipelineUnavailable(
                    "Published pipeline tenant does not match normalized record"
                ),
                started_at=started_at,
            )
        return await self._handlers[config.name].handle_record(
            envelope,
            record,
            started_at=started_at,
            source_id=source_id,
        )


def build_stream_app(
    config: VisionConfig,
    *,
    role: ServiceRole = ServiceRole.ALL,
    topics: TopicLayout | None = None,
    pipelines: Sequence[VisionConfig] | None = None,
) -> FastStream:
    """Builds the fully instrumented FastStream Kafka/Redpanda application."""
    topic_layout = topics or TopicLayout()
    registry = PipelineRuntimeRegistry(pipelines or (config,))
    pipeline_metrics = PipelineMetrics()
    resources = _resources_for_role(role)
    runtime = _Runtime(
        config=config,
        resources=resources,
        registry=registry,
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
    ingress_handler = _MultiPipelineIngress(
        registry,
        {
            pipeline.name: IngressHandler(
                pipeline=pipeline.name,
                tenant_id=pipeline.runtime_tenant_id,
                routes=registry.routes_for(pipeline.name),
                publisher=broker,
                topics=topic_layout,
                metrics=pipeline_metrics,
            )
            for pipeline in registry.configs
        },
    )

    decoder: AvroMessageDecoder | None = None
    if role in {ServiceRole.ALL, ServiceRole.INGRESS}:
        decoder = AvroMessageDecoder(
            sources=list(registry.sources),
            schema_registry_url=config.kafka.schema_registry,
        )
        _register_ingress(
            broker,
            config,
            registry.topics,
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

    timers = tuple(
        CronCommandPublisher(
            factory=ScheduledCommandFactory(
                pipeline.to_pipeline_config(),
                registry.routes_for(pipeline.name),
                tenant_id=pipeline.runtime_tenant_id,
            ),
            publisher=broker,
            topics=topic_layout,
        )
        for pipeline in registry.configs
        if registry.routes_for(pipeline.name).scheduled_steps
    )
    if role in {ServiceRole.ALL, ServiceRole.INGRESS} and timers:
        timer_stop = asyncio.Event()
        timer_tasks: tuple[asyncio.Task[None], ...] = ()

        @app.after_startup
        async def start_timer() -> None:
            nonlocal timer_tasks
            timer_tasks = tuple(
                asyncio.create_task(
                    timer.run(timer_stop),
                    name=f"pipeline-cron-publisher-{index}",
                )
                for index, timer in enumerate(timers)
            )

        @app.on_shutdown
        async def stop_timer() -> None:
            timer_stop.set()
            if timer_tasks:
                await asyncio.gather(*timer_tasks)

    return app


def _register_ingress(
    broker: KafkaBroker,
    config: VisionConfig,
    source_topics: tuple[str, ...],
    decoder: AvroMessageDecoder,
    handler: _MultiPipelineIngress,
) -> None:
    """Registers one-message Avro consumers so every Trace ID has one parent."""
    if not source_topics:
        raise ValueError(
            "At least one Kafka source topic is required for ingress"
        )

    @broker.subscriber(
        *source_topics,
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
        handlers = runtime.command_handler
        if handlers is None:
            await message.nack()
            raise RuntimeError("Command worker runtime is not initialized")
        try:
            runtime.registry.for_command(command.tenant_id, command.pipeline)
            handler = handlers[command.pipeline]
        except (KeyError, PipelineUnavailable):
            await message.reject()
            raise ValueError(
                "Command does not match a loaded tenant pipeline revision"
            ) from None
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
    if address is None:
        ray.init(
            address=None,
            namespace=config.ray.namespace,
            ignore_reinit_error=True,
            log_to_driver=False,
            num_cpus=config.ray.num_cpus,
            num_gpus=config.ray.num_gpus,
            include_dashboard=False,
        )
    else:
        ray.init(
            address=address,
            namespace=config.ray.namespace,
            ignore_reinit_error=True,
            log_to_driver=False,
        )
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
    config: VisionConfig,
    pipelines: Sequence[VisionConfig],
    resource: ResourceClass,
) -> Sequence[ActorHandle]:
    """Creates named actors whose process-local dependencies remain warm."""
    from galadril_vision.actors.processor import VisionCommandProcessor
    from galadril_vision.actors.worker import RayPipelineActor
    from galadril_vision.connectors.version import (
        build_vision_ontology_runtime,
    )

    handles: list[ActorHandle] = []
    telemetry = {
        "enabled": config.telemetry.enabled,
        "service_name": "galadril-vision",
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
            VisionCommandProcessor(
                pipelines,
                ontology_runtime=build_vision_ontology_runtime(
                    config.connectors.terminusdb
                ),
            ),
            telemetry,
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

    cluster_resources = cast(
        Callable[[], Mapping[str, object]], ray.cluster_resources
    )
    raw_gpu = cluster_resources().get("GPU", 0.0)
    detected = float(raw_gpu) if isinstance(raw_gpu, (int, float)) else 0.0
    return min(1.0, detected)
