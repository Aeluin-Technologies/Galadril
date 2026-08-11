"""Pipeline configuration schema definition using Pydantic."""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from typing import Annotated

from croniter import croniter
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

CleanStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class StepType(StrEnum):
    """Supported pipeline execution step types."""

    INFERENCE = "inference"
    RESOLVE = "resolve"
    SINK = "sink"
    DBT = "dbt"
    CAUSAL = "causal"


class TriggerType(StrEnum):
    """Supported scheduling trigger types."""

    MANUAL = "manual"
    CRON = "cron"


class RetryPolicy(BaseModel):
    """Defines the error recovery behavior for an execution node."""

    model_config = ConfigDict(
        strict=False,
        frozen=True,
        extra="ignore",
    )

    max_retries: int = Field(
        default=0, ge=0, description="Maximum number of execution retries."
    )
    delay_seconds: float = Field(
        default=5.0, ge=0.0, description="Delay between retry attempts."
    )


class StepParams(BaseModel):
    """Execution parameters passed to runtime contexts."""

    model_config = ConfigDict(
        strict=False,
        frozen=True,
        extra="allow",
    )

    trigger: TriggerType = Field(
        default=TriggerType.MANUAL,
        description="Execution trigger mode.",
    )
    cron: CleanStr | None = Field(
        default=None,
        description="Cron expression used when trigger='cron'.",
    )
    retry_policy: RetryPolicy = Field(
        default_factory=RetryPolicy,
        description="Error retry policy for this computational node.",
    )

    @model_validator(mode="after")
    def validate_trigger(self) -> StepParams:
        """Validates trigger-specific constraints and normlizes cron strings."""
        if self.trigger is TriggerType.CRON:
            if self.cron is None:
                raise ValueError(
                    "A cron expression is required when trigger='cron'."
                )
            if not croniter.is_valid(self.cron):
                raise ValueError(
                    f"Invalid cron expression format: '{self.cron}'."
                )
        elif self.cron is not None:
            raise ValueError(
                "'cron' may only be specified when trigger='cron'."
            )
        return self


class Source(BaseModel):
    """Schema validation for data ingestion sources."""

    model_config = ConfigDict(
        strict=False,
        frozen=True,
        extra="ignore",
    )

    id: CleanStr = Field(description="Unique identifier for the data source.")
    topic: CleanStr = Field(
        description="Target Kafka topic to listen for incoming events.",
        default="raw",
    )
    match_pattern: CleanStr = Field(
        description="Regex pattern to filter S3 object paths or metadata."
    )
    schema_path: CleanStr = Field(
        description="Local or remote path to the Avro schema definition."
    )


class PipelineStep(BaseModel):
    """Schema validation for execution graph steps."""

    model_config = ConfigDict(
        strict=False,
        frozen=True,
        extra="ignore",
    )

    step: CleanStr = Field(
        description="Unique name of the pipeline execution step."
    )
    type: StepType = Field(
        description="Execution type: inference, resolve, sink, dbt, or causal."
    )
    input_from: list[CleanStr] = Field(
        default_factory=list,
        description="List of upstream step or source identifiers this step depends on.",
    )
    model: CleanStr | None = Field(
        default=None, description="Fully qualified class path of the AI model."
    )
    artifact_path: CleanStr | None = Field(
        default=None, description="Storage location path of model artifacts."
    )
    params: StepParams = Field(
        default_factory=StepParams,
        description="Execution parameters passed to the step runtime context.",
    )

    @model_validator(mode="after")
    def validate_semantics(self) -> PipelineStep:
        """Validates step-specific business constraints."""
        if self.type is StepType.INFERENCE and self.model is None:
            raise ValueError(
                "Inference steps require a non-null 'model' field."
            )
        return self


class PipelineConfig(BaseModel):
    """Root configuration model representing a complete multi-tenant pipeline."""

    model_config = ConfigDict(
        strict=False,  # Backward compatibility.
        frozen=True,
        extra="ignore",
    )

    version: int = Field(
        default=1,
        ge=1,
        description="Schema evolutionary version control tracking id.",
    )
    name: CleanStr = Field(
        description="Human-readable name of the pipeline framework."
    )
    sources: list[Source] = Field(
        description="List of durable event sources consumed by the pipeline."
    )
    pipeline: list[PipelineStep] = Field(
        description="Topological collection of execution operations."
    )

    @model_validator(mode="after")
    def validate_pipeline_graph(self) -> PipelineConfig:
        """Performs cross-object validation on the pipeline graph."""
        source_ids = [source.id for source in self.sources]
        step_ids = [step.step for step in self.pipeline]

        duplicate_sources = {s for s in source_ids if source_ids.count(s) > 1}
        if duplicate_sources:
            raise ValueError(
                f"Duplicate source identifiers: {sorted(duplicate_sources)}"
            )

        duplicate_steps = {s for s in step_ids if step_ids.count(s) > 1}
        if duplicate_steps:
            raise ValueError(
                f"Duplicate step identifiers: {sorted(duplicate_steps)}"
            )

        known_nodes = set(source_ids) | set(step_ids)
        for step in self.pipeline:
            missing_dependencies = set(step.input_from) - known_nodes
            if missing_dependencies:
                raise ValueError(
                    f"Step '{step.step}' references unknown dependencies: {sorted(missing_dependencies)}"
                )

        self.get_topological_order()
        return self

    def get_topological_order(self) -> list[str]:
        """Computes and verifies the topological execution order of the graph nodes.

        Returns:
            A list of node identifiers sorted by dependency order.
        """
        graph: dict[str, list[str]] = defaultdict(list)
        in_degree: dict[str, int] = {}

        for source in self.sources:
            in_degree[source.id] = 0

        for step in self.pipeline:
            in_degree[step.step] = 0

        for step in self.pipeline:
            for dependency in step.input_from:
                graph[dependency].append(step.step)
                in_degree[step.step] += 1

        from collections import deque

        queue = deque(node for node, degree in in_degree.items() if degree == 0)
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(in_degree):
            raise ValueError(
                "Cyclic dependency detected within the execution graph."
            )

        return order
