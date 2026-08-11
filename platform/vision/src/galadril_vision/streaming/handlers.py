"""Framework-light handlers used by FastStream subscribers."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

import structlog
from galadril_pipeline.events import (
    EventStatus,
    LineageEvent,
    PipelineCommand,
    StepResult,
    utc_now,
)
from galadril_pipeline.routing import PipelineRouteTable
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from galadril_vision.common.schemas import CanonicalRecord, SchemaViolation
from galadril_vision.connectors.kafka.schemas import EventNormalizer
from galadril_vision.pipeline.ledger import ClaimState, ExecutionClaim
from galadril_vision.streaming.topics import TopicLayout
from galadril_vision.telemetry.context import (
    bind_pipeline_context,
    current_trace_identifiers,
)
from galadril_vision.telemetry.metrics import PipelineMetrics

logger = structlog.get_logger(__name__)


class AvroEnvelope(BaseModel):
    """Decoded Confluent Avro message annotated with its configured source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    payload: dict[str, JsonValue]


class Publisher(Protocol):
    """Subset of the FastStream broker publishing interface used by handlers."""

    async def publish(
        self,
        message: object,
        topic: str,
        *,
        key: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        correlation_id: str | None = None,
        no_confirm: bool = False,
    ) -> object: ...


class Dispatcher(Protocol):
    """Non-blocking Ray dispatch interface."""

    async def dispatch(self, command: PipelineCommand) -> StepResult: ...


class ExecutionLedger(Protocol):
    """Durable command claim interface."""

    async def claim(self, command: PipelineCommand) -> ExecutionClaim: ...

    async def complete(
        self, command: PipelineCommand, result: StepResult
    ) -> None: ...

    async def fail(
        self, command: PipelineCommand, error: Exception
    ) -> None: ...


class CommandInProgress(RuntimeError):
    """Signals that a live lease should be redelivered after its owner finishes."""


class IngressHandler:
    """Normalizes validated Avro records into deterministic entry commands."""

    __slots__ = ("_metrics", "_pipeline", "_publisher", "_routes", "_topics")

    def __init__(
        self,
        *,
        pipeline: str,
        routes: PipelineRouteTable,
        publisher: Publisher,
        topics: TopicLayout,
        metrics: PipelineMetrics,
    ) -> None:
        self._pipeline = pipeline
        self._routes = routes
        self._publisher = publisher
        self._topics = topics
        self._metrics = metrics

    async def handle(
        self, envelope: AvroEnvelope
    ) -> tuple[PipelineCommand, ...]:
        """Publishes entry commands only after Pydantic v2 normalization succeeds."""
        started_at = time.perf_counter()
        try:
            normalized = EventNormalizer.normalize(
                envelope.payload, envelope.source_id
            )
            record = CanonicalRecord.model_validate(normalized)
            commands = self._commands(
                envelope.source_id, envelope.topic, record
            )
        except Exception as error:
            violation = SchemaViolation(
                reason=str(error),
                record_id=_record_id(envelope.payload),
                topic=envelope.topic,
                raw=envelope.payload,
            )
            await self._publisher.publish(
                violation.model_dump(mode="json"),
                self._topics.invalid,
                key=violation.record_id,
                no_confirm=False,
            )
            self._metrics.message_completed(
                pipeline=self._pipeline,
                step="ingress",
                outcome="rejected",
                duration_seconds=time.perf_counter() - started_at,
            )
            logger.warning(
                "ingress_record_rejected",
                pipeline=self._pipeline,
                step="ingress",
                entity_id=violation.record_id,
                reason=violation.reason,
            )
            return ()

        with bind_pipeline_context(
            pipeline=self._pipeline,
            step="ingress",
            entity_id=record.record_id,
        ):
            for command in commands:
                await self._publisher.publish(
                    command.model_dump(mode="json"),
                    self._topics.commands_for(command.resource_class),
                    key=str(command.event_id),
                    correlation_id=str(command.correlation_id),
                    no_confirm=False,
                )
                await self._publish_lineage(command, EventStatus.ACCEPTED)
            logger.info(
                "ingress_record_accepted",
                source_id=envelope.source_id,
                topic=envelope.topic,
                commands_published=len(commands),
            )
        self._metrics.message_completed(
            pipeline=self._pipeline,
            step="ingress",
            outcome="accepted",
            duration_seconds=time.perf_counter() - started_at,
        )
        return commands

    def _commands(
        self,
        source_id: str,
        topic: str,
        record: CanonicalRecord,
    ) -> tuple[PipelineCommand, ...]:
        """Builds stable IDs so a replay cannot fork duplicate logical work."""
        correlation_id = uuid5(
            NAMESPACE_URL,
            f"{record.tenant_id}:{source_id}:{record.record_id}",
        )
        payload: dict[str, JsonValue] = {
            "record": record.model_dump(mode="json")
        }
        commands = []
        for step_name in self._routes.entry_steps(source_id):
            route = self._routes.route(step_name)
            commands.append(
                PipelineCommand(
                    event_id=uuid5(correlation_id, f"step:{step_name}"),
                    correlation_id=correlation_id,
                    causation_id=correlation_id,
                    tenant_id=record.tenant_id,
                    entity_id=record.record_id,
                    pipeline=self._pipeline,
                    step=route.step,
                    step_type=route.step_type,
                    resource_class=route.resource_class,
                    payload=payload,
                    attributes={
                        "source_id": source_id,
                        "source_topic": topic,
                    },
                )
            )
        return tuple(commands)

    async def _publish_lineage(
        self, command: PipelineCommand, status: EventStatus
    ) -> None:
        """Emits an append-only accepted transition with the active trace ID."""
        lineage = _lineage(command, status)
        await self._publisher.publish(
            lineage.model_dump(mode="json"),
            self._topics.lineage,
            key=str(lineage.event_id),
            correlation_id=str(command.correlation_id),
            no_confirm=False,
        )


class CommandHandler:
    """Claims, dispatches, persists, and deterministically advances commands."""

    __slots__ = (
        "_dispatcher",
        "_ledger",
        "_metrics",
        "_publisher",
        "_routes",
        "_topics",
    )

    def __init__(
        self,
        *,
        routes: PipelineRouteTable,
        publisher: Publisher,
        dispatcher: Dispatcher,
        ledger: ExecutionLedger,
        topics: TopicLayout,
        metrics: PipelineMetrics,
    ) -> None:
        self._routes = routes
        self._publisher = publisher
        self._dispatcher = dispatcher
        self._ledger = ledger
        self._topics = topics
        self._metrics = metrics

    async def handle(self, command: PipelineCommand) -> StepResult | None:
        """Completes one at-least-once command and publishes its durable successors."""
        started_at = time.perf_counter()
        with bind_pipeline_context(
            pipeline=command.pipeline,
            step=command.step,
            entity_id=command.entity_id,
        ):
            claim = await self._ledger.claim(command)
            if claim.state is ClaimState.IN_PROGRESS:
                logger.info(
                    "pipeline_command_already_in_progress",
                    event_id=str(command.event_id),
                    attempt=command.attempt,
                )
                raise CommandInProgress(command.idempotency_key)
            if claim.state is ClaimState.COMPLETED:
                if claim.result is None:
                    raise RuntimeError(
                        "Completed execution is missing its result"
                    )
                await self._publish_successors(command, claim.result)
                logger.info(
                    "pipeline_command_replayed",
                    event_id=str(command.event_id),
                    attempt=command.attempt,
                )
                return claim.result

            logger.info(
                "pipeline_command_claimed",
                event_id=str(command.event_id),
                attempt=command.attempt,
                resource_class=command.resource_class.value,
            )
            await self._publish_lineage(command, EventStatus.RUNNING)
            try:
                result = await self._dispatcher.dispatch(command)
                await self._ledger.complete(command, result)
                await self._publish_successors(command, result)
            except Exception as error:
                await self._ledger.fail(command, error)
                await self._handle_failure(command, error)
                self._metrics.message_completed(
                    pipeline=command.pipeline,
                    step=command.step,
                    outcome="failed",
                    duration_seconds=time.perf_counter() - started_at,
                )
                logger.error(
                    "pipeline_command_failed",
                    event_id=str(command.event_id),
                    attempt=command.attempt,
                    error_type=type(error).__name__,
                    error=str(error),
                )
                return None

            self._metrics.message_completed(
                pipeline=command.pipeline,
                step=command.step,
                outcome="completed",
                duration_seconds=time.perf_counter() - started_at,
            )
            logger.info(
                "pipeline_command_completed",
                event_id=str(command.event_id),
                result_id=str(result.event_id),
                attempt=command.attempt,
                duration_seconds=result.duration_seconds,
            )
            return result

    async def _publish_successors(
        self, command: PipelineCommand, result: StepResult
    ) -> None:
        """Publishes deterministic downstream commands and terminal visibility events."""
        await self._publisher.publish(
            result.model_dump(mode="json"),
            self._topics.results,
            key=str(result.event_id),
            correlation_id=str(command.correlation_id),
            no_confirm=False,
        )
        await self._publish_lineage(
            command, EventStatus.COMPLETED, result=result
        )
        for step_name in self._routes.route(command.step).downstream:
            route = self._routes.route(step_name)
            downstream = PipelineCommand(
                event_id=uuid5(command.event_id, f"step:{step_name}"),
                correlation_id=command.correlation_id,
                causation_id=result.event_id,
                tenant_id=command.tenant_id,
                entity_id=command.entity_id,
                pipeline=command.pipeline,
                step=route.step,
                step_type=route.step_type,
                resource_class=route.resource_class,
                payload=result.output,
                attributes={
                    "parent_command_id": str(command.event_id),
                    "parent_result_id": str(result.event_id),
                },
            )
            await self._publisher.publish(
                downstream.model_dump(mode="json"),
                self._topics.commands_for(route.resource_class),
                key=str(downstream.event_id),
                correlation_id=str(command.correlation_id),
                no_confirm=False,
            )

    async def _handle_failure(
        self, command: PipelineCommand, error: Exception
    ) -> None:
        """Publishes bounded retries or a terminal dead-letter event."""
        route = self._routes.route(command.step)
        if command.attempt < route.max_retries:
            retry = command.model_copy(
                update={
                    "attempt": command.attempt + 1,
                    "occurred_at": utc_now(),
                }
            )
            await self._publisher.publish(
                retry.model_dump(mode="json"),
                self._topics.commands_for(command.resource_class),
                key=str(command.event_id),
                correlation_id=str(command.correlation_id),
                no_confirm=False,
            )
            await self._publish_lineage(command, EventStatus.FAILED)
            return

        failure = StepResult(
            correlation_id=command.correlation_id,
            causation_id=command.event_id,
            tenant_id=command.tenant_id,
            entity_id=command.entity_id,
            pipeline=command.pipeline,
            command_id=command.event_id,
            step=command.step,
            step_type=command.step_type,
            resource_class=command.resource_class,
            status=EventStatus.FAILED,
            attempt=command.attempt,
            duration_seconds=0.0,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        await self._publisher.publish(
            failure.model_dump(mode="json"),
            self._topics.dead_letter,
            key=str(command.event_id),
            correlation_id=str(command.correlation_id),
            no_confirm=False,
        )
        await self._publish_lineage(command, EventStatus.FAILED, result=failure)

    async def _publish_lineage(
        self,
        command: PipelineCommand,
        status: EventStatus,
        *,
        result: StepResult | None = None,
    ) -> None:
        """Publishes deterministic command state changes for lineage consumers."""
        lineage = _lineage(command, status, result=result)
        await self._publisher.publish(
            lineage.model_dump(mode="json"),
            self._topics.lineage,
            key=str(lineage.event_id),
            correlation_id=str(command.correlation_id),
            no_confirm=False,
        )


def _lineage(
    command: PipelineCommand,
    status: EventStatus,
    *,
    result: StepResult | None = None,
) -> LineageEvent:
    """Builds a stable lineage event for replay-safe Kafka compaction consumers."""
    trace_id, _ = current_trace_identifiers()
    input_refs = (
        (str(command.causation_id),) if command.causation_id is not None else ()
    )
    output_refs = (str(result.event_id),) if result is not None else ()
    return LineageEvent(
        event_id=uuid5(
            command.event_id,
            f"lineage:{status.value}:{command.attempt}",
        ),
        correlation_id=command.correlation_id,
        causation_id=command.event_id,
        tenant_id=command.tenant_id,
        entity_id=command.entity_id,
        pipeline=command.pipeline,
        command_id=command.event_id,
        step=command.step,
        step_type=command.step_type,
        resource_class=command.resource_class,
        status=status,
        attempt=command.attempt,
        input_refs=input_refs,
        output_refs=output_refs,
        trace_id=trace_id,
        attributes=command.attributes,
    )


def _record_id(payload: Mapping[str, JsonValue]) -> str | None:
    """Extracts a safe record ID for invalid-event partitioning and logs."""
    value = payload.get("id")
    return value if isinstance(value, str) and value else None
