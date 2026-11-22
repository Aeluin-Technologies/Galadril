"""Framework-light handlers used by FastStream subscribers."""

from __future__ import annotations

import time
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from galadril_ontology import GolDenied
from galadril_pipeline.events import (
    EventStatus,
    LineageEvent,
    PipelineCommand,
    StepResult,
    utc_now,
)
from galadril_pipeline.routing import PipelineRouteTable
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.connectors.kafka.schemas import EventNormalizer
from galadril_vision.pipeline.ledger import ClaimState, ExecutionClaim
from galadril_vision.streaming.security import PublicationGate
from galadril_vision.streaming.topics import TopicLayout
from galadril_vision.telemetry.context import (
    bind_pipeline_context,
    current_trace_identifiers,
)
from galadril_vision.telemetry.metrics import PipelineMetrics

logger = structlog.get_logger(__name__)
_IDENTITY_SEGMENT = r"^[A-Za-z0-9_-]+$"


class AvroEnvelope(BaseModel):
    """Decoded Confluent Avro message annotated with its configured source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    tenant_id: str = Field(
        min_length=1, max_length=64, pattern=_IDENTITY_SEGMENT
    )
    pipeline_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTITY_SEGMENT
    )
    revision_id: str = Field(
        min_length=1, max_length=128, pattern=_IDENTITY_SEGMENT
    )
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

    __slots__ = (
        "_metrics",
        "_pipeline",
        "_publisher",
        "_publication_gate",
        "_routes",
        "_topics",
        "_tenant",
    )

    def __init__(
        self,
        *,
        pipeline: str,
        tenant_id: str | None = None,
        routes: PipelineRouteTable,
        publisher: Publisher,
        topics: TopicLayout,
        metrics: PipelineMetrics,
        publication_gate: PublicationGate | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._tenant = tenant_id
        self._routes = routes
        self._publisher = publisher
        self._topics = topics
        self._metrics = metrics
        self._publication_gate = publication_gate

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
        except Exception as error:
            return await self.reject(envelope, error, started_at=started_at)
        return await self.handle_record(envelope, record, started_at=started_at)

    async def handle_record(
        self,
        envelope: AvroEnvelope,
        record: CanonicalRecord,
        *,
        started_at: float | None = None,
        source_id: str | None = None,
    ) -> tuple[PipelineCommand, ...]:
        """Routes one already-normalized record through this immutable DAG."""
        started = started_at or time.perf_counter()
        if self._tenant is not None and record.tenant_id != self._tenant:
            return ()
        try:
            commands = self._commands(
                source_id or envelope.source_id, envelope.topic, record
            )
        except Exception as error:
            return await self.reject(envelope, error, started_at=started)

        try:
            with bind_pipeline_context(
                pipeline=self._pipeline,
                step="ingress",
                entity_id=record.record_id,
            ):
                for command in commands:
                    await self._publish(
                        command,
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
        except GolDenied:
            logger.warning("gol.publication.denied", operation="ingress")
            return ()
        self._metrics.message_completed(
            pipeline=self._pipeline,
            step="ingress",
            outcome="accepted",
            duration_seconds=time.perf_counter() - started,
        )
        return commands

    async def reject(
        self,
        envelope: AvroEnvelope,
        error: Exception,
        *,
        started_at: float | None = None,
    ) -> tuple[PipelineCommand, ...]:
        """Without a verified scope, malformed ingress has no permitted output sink."""
        del error
        self._metrics.message_completed(
            pipeline=self._pipeline,
            step="ingress",
            outcome="rejected",
            duration_seconds=time.perf_counter()
            - (started_at or time.perf_counter()),
        )
        logger.warning("ingress_record_rejected", outcome="unavailable")
        return ()

    async def _publish(
        self,
        command: PipelineCommand,
        message: object,
        topic: str,
        *,
        key: bytes | str | None = None,
        correlation_id: str | None = None,
        no_confirm: bool = False,
    ) -> None:
        """Keeps broker dispatch adjacent to its noncached release decision."""
        if self._publication_gate is None:
            raise GolDenied("GOL publication unavailable")
        async with self._publication_gate.protect(command):
            await self._publisher.publish(
                message,
                topic,
                key=key,
                correlation_id=correlation_id,
                no_confirm=no_confirm,
            )

    def _commands(
        self,
        source_id: str,
        topic: str,
        record: CanonicalRecord,
    ) -> tuple[PipelineCommand, ...]:
        """Builds stable IDs so a replay cannot fork duplicate logical work."""
        correlation_id = _record_correlation_id(record, source_id)
        payload: dict[str, JsonValue] = {
            "record": record.model_dump(mode="json")
        }
        attributes: dict[str, JsonValue] = {
            "source_id": source_id,
            "source_topic": topic,
            "input_type": record.input_type,
            "concurrent_group_id": record.concurrent_group_id,
        }
        if record.lineage is not None:
            attributes.update(
                {
                    "ingestion_id": record.lineage.ingestion_id,
                    "source_correlation_id": record.lineage.correlation_id,
                    "trace_id": record.lineage.trace_id,
                    "span_id": record.lineage.span_id,
                    "observation_idempotency_key": (
                        record.lineage.idempotency_key
                    ),
                }
            )
        commands = []
        for step_name in self._routes.entry_steps(source_id):
            route = self._routes.route(step_name)
            commands.append(
                PipelineCommand(
                    event_id=uuid5(
                        correlation_id,
                        f"pipeline:{self._pipeline}:step:{step_name}",
                    ),
                    correlation_id=correlation_id,
                    causation_id=correlation_id,
                    tenant_id=record.tenant_id,
                    admission=record.admission,
                    entity_id=record.record_id,
                    pipeline=self._pipeline,
                    step=route.step,
                    step_type=route.step_type,
                    resource_class=route.resource_class,
                    payload=payload,
                    attributes=attributes,
                )
            )
        return tuple(commands)

    async def _publish_lineage(
        self, command: PipelineCommand, status: EventStatus
    ) -> None:
        """Emits an append-only accepted transition with the active trace ID."""
        lineage = _lineage(command, status)
        await self._publish(
            command,
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
        "_publication_gate",
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
        publication_gate: PublicationGate | None = None,
    ) -> None:
        self._routes = routes
        self._publisher = publisher
        self._dispatcher = dispatcher
        self._ledger = ledger
        self._topics = topics
        self._metrics = metrics
        self._publication_gate = publication_gate

    async def handle(self, command: PipelineCommand) -> StepResult | None:
        """Denied releases never publish errors containing the rejected payload."""
        try:
            if self._publication_gate is None:
                raise GolDenied("GOL publication unavailable")
            await self._publication_gate.authorize_state(command)
            return await self._handle(command)
        except GolDenied:
            logger.warning("gol.publication.denied", operation="command")
            # The consumer must retain delivery when a live release check
            # fails. Replays reuse the durable result without repeating I/O.
            raise

    async def _publish(
        self,
        command: PipelineCommand,
        message: object,
        topic: str,
        *,
        key: bytes | str | None = None,
        correlation_id: str | None = None,
        no_confirm: bool = False,
    ) -> None:
        """Keeps broker dispatch adjacent to its noncached release decision."""
        if self._publication_gate is None:
            raise GolDenied("GOL publication unavailable")
        async with self._publication_gate.protect(command):
            await self._publisher.publish(
                message,
                topic,
                key=key,
                correlation_id=correlation_id,
                no_confirm=no_confirm,
            )

    async def _handle(self, command: PipelineCommand) -> StepResult | None:
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
                _require_result_scope(command, claim.result)
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
            try:
                await self._publish_lineage(command, EventStatus.RUNNING)
            except Exception as error:
                # Dispatch has not started, so redelivery can reclaim this lease.
                logger.warning(
                    "pipeline_command_failed",
                    event_id=str(command.event_id),
                    attempt=command.attempt,
                    error_type=type(error).__name__,
                    phase="running_lineage",
                )
                await self._ledger.fail(
                    command, RuntimeError("Pipeline execution unavailable")
                )
                raise
            try:
                result = await self._dispatcher.dispatch(command)
                _require_result_scope(command, result)
                self._routes.successors(command.step, result.output)
                await self._ledger.complete(command, result)
            except Exception as error:
                logger.error(
                    "pipeline_command_failed",
                    event_id=str(command.event_id),
                    attempt=command.attempt,
                    error_type=type(error).__name__,
                    outcome="unavailable",
                )
                await self._ledger.fail(
                    command, RuntimeError("Pipeline execution unavailable")
                )
                await self._handle_failure(command)
                self._metrics.message_completed(
                    pipeline=command.pipeline,
                    step=command.step,
                    outcome="failed",
                    duration_seconds=time.perf_counter() - started_at,
                )
                return None

            await self._publish_successors(command, result)
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
        await self._publish(
            command,
            result.model_dump(mode="json"),
            self._topics.results,
            key=str(result.event_id),
            correlation_id=str(command.correlation_id),
            no_confirm=False,
        )
        await self._publish_lineage(
            command, EventStatus.COMPLETED, result=result
        )
        for step_name in self._routes.successors(command.step, result.output):
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
                admission=result.admission,
                attributes={
                    "parent_command_id": str(command.event_id),
                    "parent_result_id": str(result.event_id),
                },
            )
            await self._publish(
                command,
                downstream.model_dump(mode="json"),
                self._topics.commands_for(route.resource_class),
                key=str(downstream.event_id),
                correlation_id=str(command.correlation_id),
                no_confirm=False,
            )

    async def _handle_failure(self, command: PipelineCommand) -> None:
        """Publishes bounded retries or a terminal dead-letter event."""
        route = self._routes.route(command.step)
        if command.attempt < route.max_retries:
            retry = command.model_copy(
                update={
                    "attempt": command.attempt + 1,
                    "occurred_at": utc_now(),
                }
            )
            await self._publish(
                command,
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
            admission=command.admission,
            status=EventStatus.FAILED,
            attempt=command.attempt,
            duration_seconds=0.0,
            error_type="PipelineExecutionError",
            error_message="Pipeline execution unavailable",
        )
        await self._publish(
            command,
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
        await self._publish(
            command,
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
    if trace_id is None:
        propagated_trace_id = command.attributes.get("trace_id")
        if isinstance(propagated_trace_id, str):
            trace_id = propagated_trace_id
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


def _require_result_scope(command: PipelineCommand, result: StepResult) -> None:
    """A cached value is usable only under its original immutable execution scope."""
    if (
        result.command_id != command.event_id
        or result.tenant_id != command.tenant_id
        or result.pipeline != command.pipeline
        or result.step != command.step
        or result.step_type != command.step_type
        or result.resource_class != command.resource_class
        or result.correlation_id != command.correlation_id
        or result.admission != command.admission
    ):
        raise GolDenied("GOL cached result unavailable")


def _record_correlation_id(record: CanonicalRecord, source_id: str) -> UUID:
    """Derives a per-observation ID under the source object's lineage ID."""
    if record.lineage is not None:
        try:
            source_correlation_id = UUID(record.lineage.correlation_id)
        except ValueError:
            pass
        else:
            # A CSV object's correlation groups its rows for lineage, but a
            # command claim must distinguish every row under that group.
            return uuid5(
                source_correlation_id,
                f"{source_id}:{record.record_id}",
            )
    return uuid5(
        NAMESPACE_URL,
        f"{record.tenant_id}:{source_id}:{record.record_id}",
    )
