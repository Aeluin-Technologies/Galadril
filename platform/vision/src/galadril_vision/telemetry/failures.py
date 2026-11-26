"""Emits only catalog fields at pipeline failure boundaries."""

from galadril_pipeline.events import Failure, PipelineCommand
from galadril_pipeline.telemetry_contract import (
    ATTR_ERROR_CODE,
    ATTR_ERROR_OCCURRENCE_ID,
    EVENT_VISION_STEP_FAILED,
    VisionPhase,
    vision_step_failed_attributes,
)
from opentelemetry.trace import Span, StatusCode

from galadril_vision.telemetry.logging import emit_contract_event


def record_failure(
    command: PipelineCommand,
    failure: Failure,
    phase: VisionPhase,
    *,
    span: Span | None = None,
    retry_scheduled: bool | None = None,
) -> None:
    """Correlates a failed attempt without serializing the exception chain."""
    occurrence_id = str(failure.occurrence_id)
    if span is not None:
        span.set_status(StatusCode.ERROR)
        span.set_attribute(ATTR_ERROR_CODE, failure.code.value)
        span.set_attribute(ATTR_ERROR_OCCURRENCE_ID, occurrence_id)
    emit_contract_event(
        EVENT_VISION_STEP_FAILED,
        "pipeline step failed",
        vision_step_failed_attributes(
            error_code=failure.code,
            error_occurrence_id=occurrence_id,
            pipeline_attempt=command.attempt,
            vision_phase=phase,
            messaging_message_id=str(command.event_id),
            vision_retry_scheduled=retry_scheduled,
        ),
    )
