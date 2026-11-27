"""Bounded GOL sources; only Registry compiles and resolves ontology modules."""

from __future__ import annotations

from typing import Annotated, Literal

from galadril_pipeline.capabilities import PluginId
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

type PluginVersionPin = Annotated[str, Field(min_length=1, max_length=64)]


class OntologyContract(BaseModel):
    """Capability requirements select logical targets, never tenant identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["optional", "required"] = "optional"
    targets: tuple[str, ...] = Field(default=(), max_length=256)


class GolSource(BaseModel):
    """A source path is descriptive and cannot select a filesystem outside a package."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=192)
    text: str = Field(min_length=1, max_length=512 * 1024)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if (
            not value.endswith(".gol")
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or not all(
                char.isascii() and (char.isalnum() or char in "_/.-")
                for char in value
            )
        ):
            raise ValueError("Invalid packaged GOL path")
        return value


class GolPackageManifest(BaseModel):
    """Version and capability metadata do not duplicate the GOL semantic model."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:[.][a-z][a-z0-9_]*)+$")
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    files: tuple[str, ...] = Field(min_length=1, max_length=32)
    requires_plugins: dict[PluginId, PluginVersionPin] = Field(
        default_factory=dict, max_length=32
    )
    contracts: dict[str, OntologyContract] = Field(
        default_factory=dict, max_length=128
    )
    bindings: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict, max_length=128
    )

    @field_validator("files")
    @classmethod
    def source_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for path in values:
            GolSource.safe_path(path)
        if len(set(values)) != len(values):
            raise ValueError("Duplicate packaged GOL path")
        return values


class OntologyPackage(BaseModel):
    """Catalog publication contains source files and explicit plugin metadata only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str
    version: str
    files: tuple[GolSource, ...] = Field(min_length=1, max_length=32)
    requires_plugins: dict[PluginId, PluginVersionPin] = Field(
        default_factory=dict, max_length=32
    )
    contracts: dict[str, OntologyContract] = Field(
        default_factory=dict, max_length=128
    )
    bindings: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict, max_length=128
    )

    @model_validator(mode="after")
    def bounded(self) -> OntologyPackage:
        if (
            sum(len(source.text.encode("utf-8")) for source in self.files)
            > 512 * 1024
        ):
            raise ValueError("Ontology package exceeds limit")
        if len({source.path for source in self.files}) != len(self.files):
            raise ValueError("Duplicate packaged GOL path")
        return self
