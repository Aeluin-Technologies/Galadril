"""Ray actor boundary with explicit W3C parent extraction."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Protocol, cast

import ray
import structlog
from galadril_pipeline.events import EventStatus, PipelineCommand, StepResult
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import JsonValue

from galadril_vision.telemetry.context import (
    bind_pipeline_context,
    start_span_from_carrier,
)
from galadril_vision.telemetry.logging import configure_logging
from galadril_vision.telemetry.tracing import configure_telemetry

logger = structlog.get_logger(__name__)


class CommandProcessor(Protocol):
    """Executes the domain behavior for a validated pipeline command."""

    async def process(
        self, command: PipelineCommand
    ) -> dict[str, JsonValue]: ...


class PipelineActor:
    """Validates, traces, and executes commands inside a Ray worker process."""

    __slots__ = ("_processor",)

    def __init__(
        self,
        processor: CommandProcessor,
        telemetry: Mapping[str, str | bool | None] | None = None,
    ) -> None:
        """Stores actor-local dependencies so model and connection pools are reused."""
        self._processor = processor
        if telemetry and bool(telemetry.get("enabled")):
            _, _, logger_provider = configure_telemetry(
                service_name=str(
                    telemetry.get("service_name") or "galadril-ray-worker"
                ),
                environment=str(telemetry.get("environment") or "production"),
                version=str(telemetry.get("version") or "1.0.0"),
                otlp_endpoint=cast(str | None, telemetry.get("otlp_endpoint")),
                otlp_insecure=bool(telemetry.get("otlp_insecure")),
            )
            configure_logging(
                enable_json_format=True,
                otlp_logger_provider=logger_provider,
            )

    async def execute(
        self,
        command_json: bytes,
        trace_carrier: Mapping[str, str],
    ) -> bytes:
        """Executes one command under the exact remote W3C trace parent."""
        command = PipelineCommand.model_validate_json(command_json)
        started_at = time.perf_counter()
        attributes = {
            "messaging.message.id": str(command.event_id),
            "galadril.pipeline": command.pipeline,
            "galadril.pipeline.step": command.step,
            "galadril.entity.id": command.entity_id or "",
            "ray.resource_class": command.resource_class.value,
        }

        with start_span_from_carrier(
            "ray.actor.execute",
            trace_carrier,
            kind=SpanKind.CONSUMER,
            attributes=attributes,
        ) as span:
            with bind_pipeline_context(
                pipeline=command.pipeline,
                step=command.step,
                entity_id=command.entity_id,
            ):
                logger.info(
                    "ray_actor_task_started",
                    event_id=str(command.event_id),
                    resource_class=command.resource_class.value,
                )
                try:
                    output = await self._processor.process(command)
                except Exception as error:
                    span.set_status(
                        Status(StatusCode.ERROR, description=str(error))
                    )
                    logger.exception(
                        "ray_actor_task_failed",
                        event_id=str(command.event_id),
                        error_type=type(error).__name__,
                    )
                    raise

                duration = time.perf_counter() - started_at
                span.set_status(Status(StatusCode.OK))
                logger.info(
                    "ray_actor_task_completed",
                    event_id=str(command.event_id),
                    duration_seconds=duration,
                )

        result = StepResult(
            correlation_id=command.correlation_id,
            causation_id=command.event_id,
            tenant_id=command.tenant_id,
            entity_id=command.entity_id,
            pipeline=command.pipeline,
            command_id=command.event_id,
            step=command.step,
            step_type=command.step_type,
            resource_class=command.resource_class,
            status=EventStatus.COMPLETED,
            attempt=command.attempt,
            duration_seconds=duration,
            output=output,
        )
        return result.model_dump_json().encode("utf-8")


RayPipelineActor = ray.remote(max_restarts=-1, max_task_retries=0)(
    PipelineActor
)
