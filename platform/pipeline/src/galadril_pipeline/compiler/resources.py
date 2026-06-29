"""System abstraction layer and Pydantic telemetry models."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class NodeStatus(str, Enum):
    """Execution state enumeration compliant with string coercion."""

    COMPLETED = "completed"
    FAILED = "failed"
    RUNNING = "running"
    SKIPPED = "skipped"


class NodeTelemetrySnapshot(BaseModel):
    """State snapshot captured at the boundaries of an asset execution node."""

    node_id: str
    status: NodeStatus
    records_mutated: int = 0
    storage_uri_pointers: List[str] = Field(default_factory=list)


class StepRuntimeInput(BaseModel):
    """Payload injected into the platform engine client invocation."""

    correlation_id: str
    step_name: str
    step_type: Any
    params: Dict[str, Any] = Field(default_factory=dict)
    upstream_states: List[NodeTelemetrySnapshot] = Field(default_factory=list)


class StepRuntimeOutput(BaseModel):
    """Contract schema returned by the execution engine back to the platform."""

    status: NodeStatus
    records_processed: int = 0
    latency_seconds: float = 0.0
    storage_uri_pointers: List[str] = Field(default_factory=list)
    error_details: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)


class AbstractStepExecutor(ABC):
    """Base interface for compiling execution backends into Dagster resource entities."""

    @abstractmethod
    async def execute_step(
        self, runtime_input: StepRuntimeInput
    ) -> StepRuntimeOutput:
        """Executes a logical pipeline step asynchronously against the target engine."""
        pass
