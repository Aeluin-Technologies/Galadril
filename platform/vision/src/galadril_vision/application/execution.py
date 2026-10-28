"""Command admission and execution without backend or transport dependencies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from galadril_ontology import (
    OntologyError,
    OntologyRuntimeManager,
    OntologySliceRequest,
)
from galadril_pipeline.config import PipelineStep, StepType
from galadril_pipeline.events import PipelineCommand
from pydantic import JsonValue

from galadril_vision.application.errors import CommandProcessingError
from galadril_vision.application.ontology import ontology_contract


class StepHandler(Protocol):
    """Runs a published step after the application has admitted its command."""

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]: ...


@dataclass(frozen=True, slots=True)
class ExecutionRoute:
    """Carries trusted composition inputs, never values selected by a payload."""

    execution_id: str
    tenant_id: str | None
    pipeline_id: str
    revision_id: str
    steps: tuple[PipelineStep, ...]
    handlers: Mapping[str, StepHandler]


@dataclass(frozen=True, slots=True)
class _BoundStep:
    step: PipelineStep
    handler: StepHandler


@dataclass(frozen=True, slots=True)
class _BoundRoute:
    tenant_id: str | None
    pipeline_id: str
    revision_id: str
    steps: Mapping[str, _BoundStep]


class PipelineExecutor:
    """Compiles admission once so commands use constant-time lookups."""

    __slots__ = ("_routes", "_ontology_runtime")

    def __init__(
        self,
        routes: Sequence[ExecutionRoute],
        *,
        ontology_runtime: OntologyRuntimeManager | None = None,
    ) -> None:
        """Rejects ambiguous or incomplete compositions before accepting work."""
        if not routes:
            raise ValueError("At least one execution route is required")
        self._routes: dict[str, _BoundRoute] = {}
        self._ontology_runtime = ontology_runtime
        for route in routes:
            if route.execution_id in self._routes:
                raise ValueError("Pipeline execution identities must be unique")
            steps = {step.step: step for step in route.steps}
            if len(steps) != len(route.steps):
                raise ValueError("Published step identities must be unique")
            if steps.keys() != route.handlers.keys():
                raise ValueError(
                    "Step handler bindings must match published steps"
                )
            self._routes[route.execution_id] = _BoundRoute(
                tenant_id=route.tenant_id,
                pipeline_id=route.pipeline_id,
                revision_id=route.revision_id,
                steps={
                    name: _BoundStep(
                        step.model_copy(deep=True), route.handlers[name]
                    )
                    for name, step in steps.items()
                },
            )

    async def process(self, command: PipelineCommand) -> dict[str, JsonValue]:
        """Binds ontology only after validating the complete execution route."""
        route = self._routes.get(command.pipeline)
        if route is None or (
            route.tenant_id is not None and command.tenant_id != route.tenant_id
        ):
            raise CommandProcessingError(
                "Command does not match a loaded tenant or revision"
            )
        binding = route.steps.get(command.step)
        if binding is None:
            raise CommandProcessingError(
                f"Unknown configured step: '{command.step}'"
            )
        if command.step_type is not binding.step.type:
            raise CommandProcessingError(
                f"Command step type '{command.step_type}' does not match '{binding.step.type}'"
            )
        if self._ontology_runtime is None or binding.step.type in {
            StepType.FORWARD,
            StepType.FILTER,
        }:
            return await binding.handler.execute(command, binding.step)
        request = OntologySliceRequest(
            tenant_id=command.tenant_id,
            pipeline_id=binding.step.origin.pipeline_id
            if binding.step.origin
            else route.pipeline_id,
            pipeline_revision_id=binding.step.origin.revision_id
            if binding.step.origin
            else route.revision_id,
            block_id=binding.step.origin.block_id
            if binding.step.origin
            else command.step,
        )
        try:
            async with self._ontology_runtime.bind(
                request, ontology_contract(binding.step)
            ):
                return await binding.handler.execute(command, binding.step)
        except OntologyError as error:
            raise CommandProcessingError(
                f"Block ontology unavailable or incompatible: {error}"
            ) from error
