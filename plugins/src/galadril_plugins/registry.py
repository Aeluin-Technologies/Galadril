"""Validated deployment inventory; tenant data cannot select executable imports."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Literal, Protocol, cast

import structlog
from galadril_pipeline.capabilities import (
    Capability,
    CapabilityCatalog,
    CapabilityKind,
    CapabilityTarget,
    PluginId,
)
from galadril_pipeline.config import PipelineConfig, PipelineStep, Source
from galadril_pipeline.events import PipelineCommand
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from galadril_plugins.validation import compile_parameters, validate_parameters

logger = structlog.get_logger(__name__)


class PluginExecutionError(RuntimeError):
    """Keeps provider exceptions and private plugin inputs out of transport logs."""


@dataclass(frozen=True, slots=True)
class PluginScope:
    """Pins plugin resources to the same trusted identity as built-in handlers."""

    tenant_id: str
    pipeline_id: str
    revision_id: str
    execution_id: str | None = None


class StepHandler(Protocol):
    """Avoids coupling the plugin SDK to Vision infrastructure imports."""

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]: ...


type HandlerFactory = Callable[[PluginScope, PipelineStep], StepHandler]


class Binding(BaseModel):
    """Only deployment manifests can name executable factories."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    pipeline: str | None = None
    scribe: str | None = None
    model: str | None = None
    skill: str | None = None
    service: str | None = None


class PluginManifest(BaseModel):
    """Rejects a partially executable inventory before constructing any clients."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    id: PluginId
    version: str = Field(min_length=1, max_length=64)
    requires: dict[PluginId, str] = Field(default_factory=dict, max_length=32)
    capabilities: tuple[Capability, ...] = Field(max_length=128)
    bindings: dict[str, Binding] = Field(max_length=128)
    services: dict[str, str] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def coherent(self) -> PluginManifest:
        catalog = CapabilityCatalog(capabilities=self.capabilities)
        if self.id in {"core", "user"} or self.id in self.requires:
            raise ValueError("Reserved or recursive plugin identity")
        if set(self.bindings) != {item.id for item in catalog.capabilities}:
            raise ValueError(
                "Every plugin capability requires an exact binding"
            )
        prefix = f"galadril_plugins.{self.id}."
        for path in (
            *self.services.values(),
            *(
                path
                for binding in self.bindings.values()
                for path in (binding.pipeline, binding.scribe, binding.model)
                if path
            ),
        ):
            if not path.startswith(prefix) or any(
                not part.isidentifier() for part in path.split(".")
            ):
                raise ValueError(
                    "Executable binding must belong to the packaged plugin"
                )
        for item in self.capabilities:
            if item.step_type in {"route", "pipeline", "forward"}:
                raise ValueError(
                    "Branch routing and transfers are native pipeline control"
                )
            if (
                item.origin != "plugin"
                or item.plugin_id != self.id
                or item.plugin_version != self.version
                or item.execution != "registered"
            ):
                raise ValueError(
                    "Plugin capability ownership or version mismatch"
                )
            binding = self.bindings[item.id]
            if CapabilityTarget.PIPELINE in item.targets:
                if item.kind is CapabilityKind.INPUT:
                    if (
                        binding.service not in self.services
                        or item.step_type != "source"
                    ):
                        raise ValueError(
                            "Input capability requires a declared service"
                        )
                elif binding.pipeline is None or item.step_type is None:
                    raise ValueError(
                        "Pipeline capability requires an execution factory"
                    )
            if CapabilityTarget.SCRIBE in item.targets:
                if item.kind is CapabilityKind.SKILL:
                    if (
                        binding.skill is None
                        or binding.skill.startswith("/")
                        or ".." in binding.skill.split("/")
                    ):
                        raise ValueError(
                            "Skill requires a packaged relative resource"
                        )
                elif item.kind is CapabilityKind.MODEL:
                    if binding.model is None:
                        raise ValueError(
                            "Scribe models require a model factory"
                        )
                elif binding.scribe is None:
                    raise ValueError(
                        "Scribe capability requires a toolset factory"
                    )
        return self


@dataclass(frozen=True, slots=True)
class PackagedSkill:
    capability: Capability
    instructions: str


@dataclass(frozen=True, slots=True)
class _ScopedHandler:
    scope: PluginScope
    step: PipelineStep
    delegate: StepHandler

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        if command.tenant_id != self.scope.tenant_id:
            raise ValueError("Plugin command tenant does not match its binding")
        if (
            (
                self.scope.execution_id is not None
                and command.pipeline != self.scope.execution_id
            )
            or step != self.step
            or command.step != self.step.step
            or command.step_type != self.step.type
        ):
            raise ValueError(
                "Plugin command does not match its published binding"
            )
        with trace.get_tracer("galadril.plugins").start_as_current_span(
            "plugin.step.execute",
            attributes={
                "capability.id": self.step.capability_id or "",
                "tenant.id": self.scope.tenant_id,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                return await self.delegate.execute(command, self.step)
            except Exception as error:
                span.set_status(StatusCode.ERROR)
                logger.warning(
                    "plugin.step.failed",
                    capability_id=self.step.capability_id,
                    error_type=type(error).__name__,
                )
                raise PluginExecutionError(
                    "Plugin step execution failed"
                ) from None


def _load(path: str) -> Callable[..., object]:
    module, name = path.rsplit(".", 1)
    value = getattr(importlib.import_module(module), name)
    if not callable(value):
        raise ValueError("Plugin binding must be callable")
    return cast(Callable[..., object], value)


class PluginRegistry:
    """Snapshots one reviewed deployment bundle for constant-time execution lookup."""

    __slots__ = (
        "catalog",
        "_manifests",
        "_bindings",
        "_factories",
        "_parameters",
        "_capabilities",
    )

    def __init__(self, manifests: Sequence[PluginManifest]) -> None:
        manifests = tuple(item.model_copy(deep=True) for item in manifests)
        self._manifests = {item.id: item for item in manifests}
        if len(self._manifests) != len(manifests) or len(manifests) > 32:
            raise ValueError("Plugin identities must be bounded and unique")
        for manifest in manifests:
            for dependency, version in manifest.requires.items():
                installed = self._manifests.get(dependency)
                if installed is None or installed.version != version:
                    raise ValueError("Plugin dependency version is unavailable")
        pending = set(self._manifests)
        while pending:
            ready = {
                identifier
                for identifier in pending
                if not pending.intersection(
                    self._manifests[identifier].requires
                )
            }
            if not ready:
                raise ValueError("Plugin dependency cycle")
            pending.difference_update(ready)
        self.catalog = CapabilityCatalog(
            capabilities=tuple(
                item for manifest in manifests for item in manifest.capabilities
            )
        )
        self._bindings = MappingProxyType(
            {
                identifier: binding
                for manifest in manifests
                for identifier, binding in manifest.bindings.items()
            }
        )
        self._factories: dict[str, Callable[..., object]] = {}
        self._capabilities = {
            item.id: item for item in self.catalog.capabilities
        }
        self._parameters = {
            item.id: compile_parameters(item.parameters_schema)
            for item in self.catalog.capabilities
        }
        logger.info(
            "plugins.loaded",
            plugin_count=len(manifests),
            capability_count=len(self.catalog.capabilities),
        )

    @classmethod
    def from_manifests(
        cls, values: Sequence[Mapping[str, object]]
    ) -> PluginRegistry:
        return cls(
            tuple(PluginManifest.model_validate(value) for value in values)
        )

    @classmethod
    def discover(cls) -> PluginRegistry:
        manifests = []
        root = files("galadril_plugins")
        for directory in sorted(root.iterdir(), key=lambda path: path.name):
            resource = directory.joinpath("plugin.json")
            if directory.is_dir() and resource.is_file():
                data = resource.read_bytes()
                if len(data) > 262144:
                    raise ValueError("Plugin manifest exceeds limit")
                manifest = PluginManifest.model_validate_json(data)
                if manifest.id != directory.name:
                    raise ValueError(
                        "Plugin package and manifest identity disagree"
                    )
                manifests.append(manifest)
        return cls(manifests)

    def services(self) -> tuple[tuple[str, str], ...]:
        """Lists deployable services without loading executable factories."""
        return tuple(
            (manifest.id, name)
            for manifest in self._manifests.values()
            for name in manifest.services
        )

    def handler(
        self,
        scope: PluginScope,
        step: PipelineStep,
        *,
        selected_ids: tuple[str, ...] | None = None,
    ) -> StepHandler:
        capability = next(
            (
                item
                for item in self.catalog.capabilities
                if item.id == step.capability_id
            ),
            None,
        )
        if (
            capability is None
            or capability.step_type != step.type.value
            or CapabilityTarget.PIPELINE not in capability.targets
        ):
            raise ValueError("Unknown or incompatible pipeline capability")
        selected = frozenset(
            selected_ids if selected_ids is not None else (capability.id,)
        )
        self._validate_selection(capability, selected, model=step.model)
        self._validate_reference(capability.id, step.capability_version)
        validate_parameters(
            self._parameters[capability.id], step.params.model_extra or {}
        )
        path = self._bindings[capability.id].pipeline
        if path is None:
            raise ValueError("Pipeline capability has no execution binding")
        factory = self._factory(path)
        snapshot = step.model_copy(deep=True)
        delegate = cast(HandlerFactory, factory)(scope, snapshot)
        if not callable(getattr(delegate, "execute", None)):
            raise ValueError("Plugin factory did not return a step handler")
        return _ScopedHandler(scope, snapshot, delegate)

    def _factory(self, path: str) -> Callable[..., object]:
        factory = self._factories.get(path)
        if factory is None:
            factory = _load(path)
            self._factories[path] = factory
        return factory

    def _validate_selection(
        self,
        capability: Capability,
        selected: frozenset[str],
        *,
        model: str | None,
    ) -> None:
        """Dependencies select other blocks without replacing their model choices."""
        if (
            capability.compatibility.allowed_models
            and model not in capability.compatibility.allowed_models
        ):
            raise ValueError("Capability does not support the selected model")
        pending = [capability]
        visited: set[str] = set()
        while pending:
            item = pending.pop()
            if item.id in visited:
                continue
            if (
                item.id not in selected
                or CapabilityTarget.PIPELINE not in item.targets
            ):
                raise ValueError(
                    "Capability dependencies must be explicitly selected"
                )
            visited.add(item.id)
            for identifier in item.compatibility.requires:
                dependency = self._capabilities.get(identifier)
                if dependency is None:
                    raise ValueError("Capability dependency is unavailable")
                pending.append(dependency)

    def _validate_reference(
        self, identifier: str, version: str | None
    ) -> Capability:
        capability = self._capabilities.get(identifier)
        if (
            capability is None
            or CapabilityTarget.PIPELINE not in capability.targets
        ):
            raise ValueError("Unknown pipeline capability")
        if version is not None and capability.plugin_version != version:
            raise ValueError("Pipeline capability version is unavailable")
        return capability

    def validate_input(
        self, identifier: str, source: Source | None = None
    ) -> None:
        capability = self._validate_reference(
            identifier, source.capability_version if source else None
        )
        if capability.kind is not CapabilityKind.INPUT:
            raise ValueError("Unknown input capability")
        if source is not None:
            parameters = source.model_dump(
                mode="json",
                exclude={"id", "capability_id", "capability_version"},
                exclude_none=True,
            )
            validate_parameters(self._parameters[identifier], parameters)

    def validate_pipeline(self, definition: Mapping[str, object]) -> None:
        """Checks authoring choices without importing another runtime's clients."""
        if not set(definition).issubset(
            {"name", "version", "sources", "pipeline"}
        ):
            raise ValueError("Pipeline definitions cannot configure services")
        pipeline = PipelineConfig.model_validate(definition)
        items: tuple[Source | PipelineStep, ...] = (
            *pipeline.sources,
            *pipeline.pipeline,
        )
        selected = frozenset(
            item.capability_id for item in items if item.capability_id
        )
        for source in pipeline.sources:
            if source.capability_id:
                self.validate_input(source.capability_id, source)
                capability = self._capabilities[source.capability_id]
                self._validate_selection(capability, selected, model=None)
        for step in pipeline.pipeline:
            if step.capability_id:
                capability = self._validate_reference(
                    step.capability_id, step.capability_version
                )
                if capability.step_type != step.type.value:
                    raise ValueError("Incompatible pipeline capability")
                self._validate_selection(capability, selected, model=step.model)
                validate_parameters(
                    self._parameters[capability.id],
                    step.params.model_extra or {},
                )

    def skills(self) -> tuple[PackagedSkill, ...]:
        result = []
        for manifest in self._manifests.values():
            for capability in manifest.capabilities:
                path = manifest.bindings[capability.id].skill
                if path is not None:
                    instructions = (
                        files(f"galadril_plugins.{manifest.id}")
                        .joinpath(path)
                        .read_text(encoding="utf-8")
                    )
                    if (
                        not instructions.strip()
                        or len(instructions.encode()) > 32768
                    ):
                        raise ValueError(
                            "Packaged Skill exceeds limit or is empty"
                        )
                    result.append(PackagedSkill(capability, instructions))
        return tuple(result)

    def toolsets(self) -> tuple[tuple[Capability, Callable[[], object]], ...]:
        return tuple(
            (capability, cast(Callable[[], object], self._factory(path)))
            for capability in self.catalog.capabilities
            if (path := self._bindings[capability.id].scribe) is not None
        )

    def models(self) -> tuple[tuple[Capability, Callable[..., object]], ...]:
        return tuple(
            (capability, self._factory(path))
            for capability in self.catalog.capabilities
            if CapabilityTarget.SCRIBE in capability.targets
            and capability.kind is CapabilityKind.MODEL
            and (path := self._bindings[capability.id].model) is not None
        )

    def service(self, plugin: str, name: str) -> Callable[..., object]:
        manifest = self._manifests.get(plugin)
        path = manifest.services.get(name) if manifest is not None else None
        if path is None:
            raise ValueError("Unknown plugin service")
        return self._factory(path)
