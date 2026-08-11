"""Versioned contracts shared by the streaming gateway and Ray workers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

from galadril_pipeline.config import StepType

EventString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


def utc_now() -> datetime:
    """Returns an aware UTC timestamp for event envelopes."""
    return datetime.now(UTC)


class ResourceClass(StrEnum):
    """Execution pools used to isolate workloads with different constraints."""

    CPU = "cpu"
    GPU = "gpu"
    CAUSAL = "causal"


class EventStatus(StrEnum):
    """Terminal and non-terminal states emitted by a pipeline step."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class EventEnvelope(BaseModel):
    """Common identity and causality fields for all durable events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    event_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    causation_id: UUID | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    tenant_id: EventString = "default"
    entity_id: EventString | None = None
    pipeline: EventString

    @field_validator("occurred_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Normalizes event timestamps to UTC and rejects naive values."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)


class PipelineCommand(EventEnvelope):
    """A small, idempotent command dispatched to one pipeline step."""

    step: EventString
    step_type: StepType
    resource_class: ResourceClass
    attempt: int = Field(default=0, ge=0)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        """Returns the stable key used to claim a step exactly once logically."""
        return f"{self.event_id}:{self.pipeline}:{self.step}"


class StepResult(EventEnvelope):
    """Durable outcome produced after a Ray actor finishes a command."""

    command_id: UUID
    step: EventString
    step_type: StepType
    resource_class: ResourceClass
    status: EventStatus
    attempt: int = Field(default=0, ge=0)
    duration_seconds: float = Field(ge=0.0)
    output: dict[str, JsonValue] = Field(default_factory=dict)
    error_type: EventString | None = None
    error_message: str | None = Field(default=None, max_length=4096)


class LineageEvent(EventEnvelope):
    """Append-only execution transition used for operational data lineage."""

    command_id: UUID
    step: EventString
    step_type: StepType
    resource_class: ResourceClass
    status: EventStatus
    attempt: int = Field(default=0, ge=0)
    input_refs: tuple[EventString, ...] = ()
    output_refs: tuple[EventString, ...] = ()
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
