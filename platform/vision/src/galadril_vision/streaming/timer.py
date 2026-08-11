"""Deterministic cron-to-Kafka publisher for scheduled pipeline commands."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import structlog
from croniter import croniter
from galadril_pipeline.config import PipelineConfig, TriggerType
from galadril_pipeline.events import (
    EventStatus,
    LineageEvent,
    PipelineCommand,
)
from galadril_pipeline.routing import PipelineRouteTable
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import JsonValue

from galadril_vision.streaming.handlers import Publisher
from galadril_vision.streaming.topics import TopicLayout
from galadril_vision.telemetry.context import current_trace_identifiers

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """Precompiled timer metadata for one scheduled pipeline step."""

    step: str
    expression: str
    targets: tuple[str | None, ...]


class ScheduledCommandFactory:
    """Builds deterministic causal commands from validated cron configuration."""

    __slots__ = ("_pipeline", "_routes", "_schedules")

    def __init__(
        self, config: PipelineConfig, routes: PipelineRouteTable
    ) -> None:
        """Precomputes schedules and rejects incomplete cron definitions."""
        schedules = []
        for step in config.pipeline:
            if step.params.trigger is not TriggerType.CRON:
                continue
            if step.params.cron is None:
                raise ValueError(
                    f"Scheduled step '{step.step}' is missing cron"
                )
            extra = step.params.model_extra or {}
            configured_targets = extra.get("targets")
            targets = (
                tuple(
                    target
                    for target in configured_targets
                    if isinstance(target, str) and target
                )
                if isinstance(configured_targets, list)
                else ()
            )
            schedules.append(
                CronSchedule(
                    step=step.step,
                    expression=step.params.cron,
                    targets=targets or (None,),
                )
            )
        self._pipeline = config.name
        self._routes = routes
        self._schedules = tuple(schedules)

    @property
    def schedules(self) -> tuple[CronSchedule, ...]:
        """Returns immutable schedules for the runtime timer loop."""
        return self._schedules

    def commands_for(
        self, schedule: CronSchedule, scheduled_for: datetime
    ) -> tuple[PipelineCommand, ...]:
        """Creates replay-stable IDs for each scheduled target."""
        if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
            raise ValueError("scheduled_for must include a timezone")
        scheduled_utc = scheduled_for.astimezone(UTC)
        route = self._routes.route(schedule.step)
        commands = []
        for target in schedule.targets:
            identity = (
                f"{self._pipeline}:{schedule.step}:{target or '-'}:"
                f"{scheduled_utc.isoformat()}"
            )
            correlation_id = uuid5(NAMESPACE_URL, identity)
            payload: dict[str, JsonValue] = {
                "scheduled_for": scheduled_utc.isoformat()
            }
            if target is not None:
                payload["target"] = target
            commands.append(
                PipelineCommand(
                    event_id=uuid5(correlation_id, f"step:{schedule.step}"),
                    correlation_id=correlation_id,
                    causation_id=correlation_id,
                    pipeline=self._pipeline,
                    entity_id=target,
                    step=route.step,
                    step_type=route.step_type,
                    resource_class=route.resource_class,
                    payload=payload,
                )
            )
        return tuple(commands)


class CronCommandPublisher:
    """Publishes due cron events with confirmed Kafka writes and trace context."""

    __slots__ = ("_factory", "_publisher", "_topics")

    def __init__(
        self,
        *,
        factory: ScheduledCommandFactory,
        publisher: Publisher,
        topics: TopicLayout,
    ) -> None:
        self._factory = factory
        self._publisher = publisher
        self._topics = topics

    async def run(self, stop: asyncio.Event) -> None:
        """Waits cooperatively and publishes every due deterministic timer event."""
        next_runs = {
            schedule: _next_run(schedule.expression, datetime.now(UTC))
            for schedule in self._factory.schedules
        }
        while next_runs and not stop.is_set():
            next_due = min(next_runs.values())
            timeout = max((next_due - datetime.now(UTC)).total_seconds(), 0.0)
            try:
                await asyncio.wait_for(stop.wait(), timeout=timeout)
                continue
            except TimeoutError:
                pass

            now = datetime.now(UTC)
            due = tuple(
                schedule
                for schedule, scheduled_for in next_runs.items()
                if scheduled_for <= now
            )
            for schedule in due:
                scheduled_for = next_runs[schedule]
                await self._publish(schedule, scheduled_for)
                next_runs[schedule] = _next_run(schedule.expression, now)

    async def _publish(
        self, schedule: CronSchedule, scheduled_for: datetime
    ) -> None:
        """Emits a command and accepted lineage event in one traced timer scope."""
        tracer = trace.get_tracer("galadril.pipeline.timer")
        with tracer.start_as_current_span(
            "pipeline.timer.publish",
            kind=SpanKind.PRODUCER,
            attributes={"galadril.pipeline.step": schedule.step},
        ):
            for command in self._factory.commands_for(schedule, scheduled_for):
                await self._publisher.publish(
                    command.model_dump(mode="json"),
                    self._topics.commands_for(command.resource_class),
                    key=str(command.event_id),
                    correlation_id=str(command.correlation_id),
                    no_confirm=False,
                )
                trace_id, _ = current_trace_identifiers()
                lineage = LineageEvent(
                    event_id=uuid5(command.event_id, "lineage:accepted:0"),
                    correlation_id=command.correlation_id,
                    causation_id=command.event_id,
                    entity_id=command.entity_id,
                    pipeline=command.pipeline,
                    command_id=command.event_id,
                    step=command.step,
                    step_type=command.step_type,
                    resource_class=command.resource_class,
                    status=EventStatus.ACCEPTED,
                    trace_id=trace_id,
                )
                await self._publisher.publish(
                    lineage.model_dump(mode="json"),
                    self._topics.lineage,
                    key=str(lineage.event_id),
                    correlation_id=str(command.correlation_id),
                    no_confirm=False,
                )
                logger.info(
                    "scheduled_command_published",
                    pipeline=command.pipeline,
                    step=command.step,
                    entity_id=command.entity_id,
                    scheduled_for=scheduled_for.isoformat(),
                )


def _next_run(expression: str, after: datetime) -> datetime:
    """Returns an aware UTC cron occurrence strictly after the reference time."""
    next_run = croniter(expression, after).get_next(datetime)
    if not isinstance(next_run, datetime):
        raise TypeError("croniter returned a non-datetime occurrence")
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=UTC)
    return next_run.astimezone(UTC)
