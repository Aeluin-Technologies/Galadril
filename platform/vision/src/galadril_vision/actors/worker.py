"""Ray actor boundary with explicit W3C parent extraction."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Protocol, cast

import ray
from galadril_pipeline.events import (
    EventStatus,
    Failure,
    PipelineCommand,
    StepResult,
)
from galadril_pipeline.telemetry_contract import (
    SPAN_VISION_STEP_EXECUTE,
    VisionPhase,
    vision_step_execute_attributes,
)
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import JsonValue

from galadril_vision.application.errors import execution_failure
from galadril_vision.telemetry.context import (
    bind_pipeline_context,
    start_span_from_carrier,
)
from galadril_vision.telemetry.failures import record_failure
from galadril_vision.telemetry.logging import configure_logging
from galadril_vision.telemetry.tracing import configure_telemetry


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
        failure: Failure | None = None
        output: dict[str, JsonValue] = {}

        with start_span_from_carrier(
            SPAN_VISION_STEP_EXECUTE,
            trace_carrier,
            kind=SpanKind.CONSUMER,
            attributes=vision_step_execute_attributes(
                messaging_message_id=str(command.event_id),
                pipeline_attempt=command.attempt,
                vision_resource_class=command.resource_class,
            ),
        ) as span:
            with bind_pipeline_context(
                pipeline=command.pipeline,
                step=command.step,
                entity_id=command.entity_id,
            ):
                try:
                    output = await self._processor.process(command)
                except Exception as error:
                    failure = execution_failure(error, VisionPhase.EXECUTE)
                    record_failure(command, failure, failure.phase, span=span)
                else:
                    span.set_status(StatusCode.OK)

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
            admission=command.admission,
            status=EventStatus.FAILED if failure else EventStatus.COMPLETED,
            attempt=command.attempt,
            duration_seconds=time.perf_counter() - started_at,
            failure=failure,
            output=output,
        )
        return result.model_dump_json().encode("utf-8")


RayPipelineActor = ray.remote(max_restarts=-1, max_task_retries=0)(
    PipelineActor
)
