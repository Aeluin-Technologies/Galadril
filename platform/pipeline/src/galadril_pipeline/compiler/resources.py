"""Defines generic interfaces and tracking models for step executions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar
from pydantic import BaseModel, ConfigDict, Field

from galadril_pipeline.runtime.batch import BatchHandle
from galadril_pipeline.runtime.schemas import NodeStatus

T = TypeVar("T")
U = TypeVar("U")


class NodeTelemetrySnapshot(BaseModel):
    """Captured telemetry matrix boundary state for platform auditing."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    status: NodeStatus
    records_mutated: int = 0
    storage_uri_pointers: list[str] = Field(default_factory=list)


class StepRuntimeInput(BaseModel, Generic[T]):
    """Runtime generic payload state injected inside step execution engines."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    step_name: str
    step_type: str
    batch: BatchHandle[T]
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    upstream_states: list[NodeTelemetrySnapshot] = Field(default_factory=list)


class StepRuntimeOutput(BaseModel, Generic[U]):
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
