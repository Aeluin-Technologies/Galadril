"""Runtime validation schemas for state persistence and telemetry tracking."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from galadril_pipeline.config import CleanStr
from galadril_pipeline.runtime.batch import BatchHandle

T = TypeVar("T")
U = TypeVar("U")


class NodeStatus(StrEnum):
    """Execution state enumeration for functional asset node boundaries."""

    COMPLETED = "completed"
    FAILED = "failed"
    RUNNING = "running"
    SKIPPED = "skipped"


class NodeTelemetrySnapshot(BaseModel):
    """Captured telemetry matrix boundary state for platform auditing."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    status: NodeStatus
    records_mutated: int = 0
    storage_uri_pointers: list[str] = Field(default_factory=list)


class StepRuntimeInput[T](BaseModel):
    """Runtime generic payload state injected inside step execution engines."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    step_name: str
    step_type: str
    batch: BatchHandle[T]
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    upstream_states: list[NodeTelemetrySnapshot] = Field(default_factory=list)


class StepRuntimeOutput[U](BaseModel):
    """Standard execution generic contract returned to the orchestration layer."""

    model_config = ConfigDict(frozen=True)

    status: NodeStatus
    batch: BatchHandle[U]
    records_processed: int = 0
    latency_seconds: float = 0.0
    storage_uri_pointers: list[str] = Field(default_factory=list)
    error_details: str | None = None
    metrics: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AbstractStepExecutor(ABC):
    """Enforces base architectural interface for lower-level backend engines."""

    __slots__ = ()

    @abstractmethod
    async def execute_step(
        self,
        runtime_input: StepRuntimeInput[
            list[dict[str, str | int | float | bool]]
        ],
    ) -> StepRuntimeOutput[list[dict[str, str | int | float | bool]]]:
        """Executes a business computational layer action asynchronously against the engine."""


class StepCheckpoint(BaseModel):
    """Immutable checkpoint record ensuring processing idempotence and replay integrity."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    step_name: CleanStr = Field(
        description="Unique identifier of the pipeline step."
    )
    correlation_id: CleanStr = Field(
        description="Distributed trace tracking identifier."
    )
    status: NodeStatus = Field(description="Final runtime resolution status.")
    completed_at: datetime = Field(
        description="UTC timestamp marking execution completion."
    )
    records_processed: int = Field(
        ge=0, description="Total records mutated during execution."
    )
    storage_uri_pointers: list[CleanStr] = Field(
        default_factory=list,
        description="Pointers to immutable artifacts written to storage.",
    )
    payload_checksum: CleanStr = Field(
        description="Cryptographic SHA-256 signature validating state boundary integrity."
    )

    @field_validator("payload_checksum")
    @classmethod
    def validate_checksum_format(cls, value: str) -> str:
        """Validates that the payload checksum is a valid SHA-256 hexadecimal string."""
        if len(value) != 64 or not all(
            c in "0123456789abcdefABCDEF" for c in value
        ):
            raise ValueError(
                "Payload checksum must be a 64-character hexadecimal string."
            )
        return value.lower()


class PipelineRunContext(BaseModel):
    """Global immutable run-scoped token containing execution tracking metadata."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    run_id: CleanStr = Field(
        description="Unique UUID string identifying this pipeline run."
    )
    correlation_id: CleanStr = Field(
        description="Global distributed trace correlation token."
    )
    tenant_id: CleanStr = Field(
        description="Isolated tenant workspace context identifier."
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Execution initialization UTC timestamp.",
    )
