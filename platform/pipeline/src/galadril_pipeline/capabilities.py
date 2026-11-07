"""Versioned deployment metadata shared by execution and discovery adapters."""

from __future__ import annotations

from enum import StrEnum
from functools import cache
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

CapabilityId = Annotated[
    str,
    Field(max_length=128, pattern=r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$"),
]
PluginId = Annotated[str, Field(max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]


class CapabilityKind(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    MODEL = "model"
    MCP = "mcp"
    SKILL = "skill"
    ALGORITHM = "algorithm"


class CapabilityTarget(StrEnum):
    PIPELINE = "pipeline"
    SCRIBE = "scribe"


class Compatibility(BaseModel):
    """Uses exact runtime model identifiers rather than provider-dependent guesses."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    allowed_models: tuple[
        Annotated[str, Field(min_length=1, max_length=256)], ...
    ] = Field(default=(), max_length=32)
    requires: tuple[CapabilityId, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def unique_constraints(self) -> Compatibility:
        if len(set(self.allowed_models)) != len(self.allowed_models) or len(
            set(self.requires)
        ) != len(self.requires):
            raise ValueError("Compatibility constraints must be unique")
        return self


class ExecutionContext(BaseModel):
    """A registered context narrows host access without granting new authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: CapabilityId
    name: str = Field(min_length=1, max_length=128)
    aliases: tuple[str, ...] = Field(default=(), max_length=8)
    instructions: str = Field(default="", max_length=8192)
    privacy: Literal["standard", "pseudonymized"] = "standard"

    @model_validator(mode="after")
    def bounded_aliases(self) -> ExecutionContext:
        if len(set(self.aliases)) != len(self.aliases) or any(
            not alias
            or len(alias) > 128
            or not alias.isascii()
            or not all(c.isalnum() or c in "._-" for c in alias)
            for alias in self.aliases
        ):
            raise ValueError("Invalid execution context aliases")
        return self


class ExecutionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contexts: tuple[ExecutionContext, ...] = Field(default=(), max_length=8)
    requires_context: CapabilityId | None = None
    isolation: Literal["standard", "sealed"] = "standard"


class Capability(BaseModel):
    """Keeps plugin identity separate from configurable infrastructure secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: CapabilityId
    origin: Literal["plugin", "user"] = "plugin"
    plugin_id: PluginId | None = None
    plugin_version: str | None = Field(
        default=None, min_length=1, max_length=64
    )
    execution: Literal["registered", "declared"] = "registered"
    compatibility: Compatibility = Field(default_factory=Compatibility)
    execution_profile: ExecutionProfile = Field(
        default_factory=ExecutionProfile
    )
    kind: CapabilityKind
    targets: tuple[CapabilityTarget, ...] = Field(min_length=1, max_length=2)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4096)
    parameters_schema: dict[str, JsonValue]
    step_type: (
        Literal[
            "source",
            "inference",
            "resolve",
            "sink",
            "causal",
            "dbt",
            "algorithm",
            "route",
            "pipeline",
        ]
        | None
    ) = None
    model_class: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def coherent_metadata(self) -> Capability:
        """Rejects ambiguous ownership and schemas unsuitable for configuration forms."""
        if self.execution_profile != ExecutionProfile():
            if self.origin != "plugin" or self.plugin_id is None:
                raise ValueError(
                    "Execution profiles require registered plugin provenance"
                )
            if any(
                not context.id.startswith(self.plugin_id + ".")
                for context in self.execution_profile.contexts
            ):
                raise ValueError("Execution context must belong to its plugin")
            if (
                self.execution_profile.requires_context is not None
                and not self.execution_profile.requires_context.startswith(
                    self.plugin_id + "."
                )
            ):
                raise ValueError("Execution context must belong to its plugin")
            if (
                self.execution_profile.isolation == "sealed"
                and self.kind != CapabilityKind.SKILL
            ):
                raise ValueError("Sealed execution requires a Skill")
        if self.origin == "plugin":
            if (
                self.plugin_id is None
                or self.plugin_version is None
                or self.plugin_id == "user"
                or not self.id.startswith(self.plugin_id + ".")
            ):
                raise ValueError(
                    "Capability ID must belong to its plugin namespace"
                )
        elif (
            self.plugin_id is not None
            or self.plugin_version is not None
            or not self.id.startswith("user.")
        ):
            raise ValueError(
                "User capabilities require the reserved user namespace without plugin identity"
            )
        if self.id in self.compatibility.requires:
            raise ValueError("A capability cannot require itself")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("Capability targets must be unique")
        if self.parameters_schema.get("type") != "object":
            raise ValueError("Capability parameters must use an object schema")
        if (self.kind == CapabilityKind.MODEL) != (
            self.model_class is not None
        ):
            raise ValueError("Only model capabilities require a model class")
        if (
            len(self.model_dump_json(include={"parameters_schema"}).encode())
            > 16384
        ):
            raise ValueError("Capability schema exceeds limit")
        return self


class CapabilityCatalog(BaseModel):
    """Bounds public discovery independently of the number of future plugins."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    capabilities: tuple[Capability, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def bounded_unique_catalog(self) -> CapabilityCatalog:
        if len({item.id for item in self.capabilities}) != len(
            self.capabilities
        ):
            raise ValueError("Capability IDs must be unique")
        if len(self.model_dump_json().encode()) > 262144:
            raise ValueError("Capability catalog exceeds limit")
        contexts: dict[str, ExecutionContext] = {}
        aliases: dict[str, str] = {}
        for item in self.capabilities:
            for context in item.execution_profile.contexts:
                if context.id in contexts and contexts[context.id] != context:
                    raise ValueError(
                        "Conflicting execution context definitions"
                    )
                contexts[context.id] = context
                for alias in (context.id, *context.aliases):
                    if alias in aliases and aliases[alias] != context.id:
                        raise ValueError("Ambiguous execution context alias")
                    aliases[alias] = context.id
        if any(
            item.execution_profile.requires_context is not None
            and item.execution_profile.requires_context not in contexts
            for item in self.capabilities
        ):
            raise ValueError("Unregistered execution context")
        return self

    def resolve_execution_context(self, value: str) -> ExecutionContext:
        for item in self.capabilities:
            for context in item.execution_profile.contexts:
                if value == context.id or value in context.aliases:
                    return context
        raise ValueError("Unknown execution context")

    def execution_allowed(self, identifier: str, context: str | None) -> bool:
        return any(
            item.id == identifier
            and item.execution_profile.requires_context == context
            for item in self.capabilities
        )

    def validate_selection(
        self,
        identifiers: tuple[str, ...],
        *,
        target: CapabilityTarget | str,
        model: str | None,
    ) -> None:
        """Checks the complete selection without creating clients or loading code."""
        selected = frozenset(identifiers)
        if len(selected) != len(identifiers):
            raise ValueError("Duplicate capability selection")
        remaining = set(selected)
        for item in self.capabilities:
            if item.id not in selected:
                continue
            remaining.remove(item.id)
            if item.execution != "registered" or target not in item.targets:
                raise ValueError(
                    "Capability has no registered execution binding for this target"
                )
            if (
                item.compatibility.allowed_models
                and model not in item.compatibility.allowed_models
            ):
                raise ValueError(
                    "Capability does not support the selected model"
                )
            if not selected.issuperset(item.compatibility.requires):
                raise ValueError(
                    "Capability dependencies must be explicitly selected"
                )
        if remaining:
            raise ValueError("Unknown capability selection")


@cache
def builtin_catalog() -> CapabilityCatalog:
    """Reads metadata without importing models or contacting infrastructure."""
    return CapabilityCatalog.model_validate_json(
        files("galadril_pipeline")
        .joinpath("builtin_capabilities.json")
        .read_bytes()
    )
