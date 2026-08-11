"""Allocation-conscious route compilation for the event-driven pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from galadril_pipeline.config import PipelineConfig, StepType, TriggerType
from galadril_pipeline.events import ResourceClass


class RouteCompilationError(ValueError):
    """Raised when a configured graph has ambiguous streaming semantics."""


@dataclass(frozen=True, slots=True)
class StepRoute:
    """Precomputed immutable routing information for one execution step."""

    step: str
    step_type: StepType
    resource_class: ResourceClass
    dependencies: tuple[str, ...]
    downstream: tuple[str, ...]
    scheduled: bool
    max_retries: int


class PipelineRouteTable:
    """Provides constant-time source and step routing without per-event graph walks."""

    __slots__ = ("_routes", "_scheduled", "_source_entries")

    def __init__(self, config: PipelineConfig) -> None:
        """Compiles and validates streaming routes from a pipeline definition."""
        step_by_id = {step.step: step for step in config.pipeline}
        # Lists are used only during startup compilation; runtime routes are tuples.
        mutable_downstream: dict[str, list[str]] = {
            node: []
            for node in (*[source.id for source in config.sources], *step_by_id)
        }
        for step in config.pipeline:
            is_scheduled = step.params.trigger is TriggerType.CRON
            if not is_scheduled and len(step.input_from) != 1:
                raise RouteCompilationError(
                    f"Streaming step '{step.step}' must have exactly one input; "
                    "declare an explicit stateful join before using multiple inputs"
                )
            if is_scheduled:
                continue
            mutable_downstream[step.input_from[0]].append(step.step)

        routes: dict[str, StepRoute] = {}
        for step in config.pipeline:
            routes[step.step] = StepRoute(
                step=step.step,
                step_type=step.type,
                resource_class=_resource_class(step.type),
                dependencies=tuple(step.input_from),
                downstream=tuple(mutable_downstream[step.step]),
                scheduled=step.params.trigger is TriggerType.CRON,
                max_retries=step.params.retry_policy.max_retries,
            )

        source_entries = {
            source.id: tuple(mutable_downstream[source.id])
            for source in config.sources
        }
        scheduled = tuple(
            step.step
            for step in config.pipeline
            if step.params.trigger is TriggerType.CRON
        )
        self._routes: Mapping[str, StepRoute] = MappingProxyType(routes)
        self._source_entries: Mapping[str, tuple[str, ...]] = MappingProxyType(
            source_entries
        )
        self._scheduled = scheduled

    @property
    def scheduled_steps(self) -> tuple[str, ...]:
        """Returns steps started by the deterministic timer publisher."""
        return self._scheduled

    def route(self, step: str) -> StepRoute:
        """Returns an immutable route or raises a clear configuration error."""
        try:
            return self._routes[step]
        except KeyError as error:
            raise RouteCompilationError(
                f"Unknown pipeline step: '{step}'"
            ) from error

    def entry_steps(self, source: str) -> tuple[str, ...]:
        """Returns immediate event-driven consumers for a source."""
        try:
            return self._source_entries[source]
        except KeyError as error:
            raise RouteCompilationError(
                f"Unknown pipeline source: '{source}'"
            ) from error


def _resource_class(step_type: StepType) -> ResourceClass:
    """Maps semantic steps to independently scalable execution pools."""
    if step_type is StepType.INFERENCE:
        return ResourceClass.GPU
    if step_type is StepType.CAUSAL:
        return ResourceClass.CAUSAL
    return ResourceClass.CPU
