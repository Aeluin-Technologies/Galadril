"""Authorized GOL transport projections; Rust remains the semantic authority."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from .identity import normalize_tenant_id, validate_symbol_key


class Transport(BaseModel):
    """Rejects unexpected fields at the security transport boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        hide_input_in_errors=True,
    )


class GolSymbolKind(StrEnum):
    """Carries canonical categories without duplicating semantic analysis."""

    ENTITY = "entity"
    RELATION = "relation"
    FIELD = "field"
    ATTRIBUTE = "attribute"
    STRUCT = "struct"
    ENUM = "enum"
    TYPE = "type"
    INVARIANT = "invariant"
    DERIVE = "derive"
    VIEW = "view"
    ACTION = "action"


class AuthorizedSymbol(Transport):
    """Only metadata admitted by the shared Rust reader crosses this boundary."""

    key: str = Field(max_length=512)
    name: str = Field(min_length=1, max_length=128)
    kind: GolSymbolKind
    version: str = Field(min_length=1, max_length=128)
    references: tuple[str, ...] = Field(default=(), max_length=1024)
    properties: dict[str, JsonValue] = Field(
        default_factory=dict, max_length=1024
    )
    roles: dict[str, str] = Field(default_factory=dict, max_length=64)

    @field_validator("key")
    @classmethod
    def symbol_key(cls, value: str) -> str:
        return validate_symbol_key(value)


class GolProjection(Transport):
    """A data projection never conveys a grant to execute another operation."""

    tenant_id: str = Field(alias="tenantId")
    ontology_id: str = Field(alias="ontologyId", min_length=1, max_length=128)
    ontology_version: str = Field(
        alias="ontologyVersion", min_length=1, max_length=128
    )
    source_revision: str = Field(
        alias="sourceRevision", min_length=1, max_length=128
    )
    generation_id: str = Field(
        alias="generationId", min_length=1, max_length=128
    )
    symbols: tuple[AuthorizedSymbol, ...] = Field(default=(), max_length=4096)
    schemas: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict, max_length=1024
    )

    @field_validator("tenant_id")
    @classmethod
    def tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @model_validator(mode="after")
    def unique_symbols(self) -> GolProjection:
        if len({symbol.key for symbol in self.symbols}) != len(self.symbols):
            raise ValueError("Duplicate authorized GOL symbol")
        return self

    def symbol(self, key: str) -> AuthorizedSymbol | None:
        """Lookup does not resolve modules or expand the reference graph."""
        return next(
            (symbol for symbol in self.symbols if symbol.key == key), None
        )


class GolLease(GolProjection):
    """The calculation handle must never enter model prompts or public payloads."""

    context_id: str = Field(
        alias="contextId",
        min_length=1,
        max_length=256,
        repr=False,
        exclude=True,
    )

    def projection(self) -> GolProjection:
        """Removes opaque execution handles before model or API serialization."""
        return GolProjection.model_validate(self.model_dump())


class GolRequest(Transport):
    """Binds a calculation to its exact source object and published block."""

    tenant_id: str = Field(alias="tenantId")
    pipeline_id: str = Field(alias="pipelineId", min_length=1, max_length=128)
    pipeline_revision_id: str = Field(
        alias="pipelineRevisionId", min_length=1, max_length=128
    )
    block_id: str = Field(alias="blockId", min_length=1, max_length=128)
    source_object_id: str = Field(
        alias="sourceObjectId", min_length=1, max_length=4096
    )

    @field_validator("tenant_id")
    @classmethod
    def tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @model_validator(mode="after")
    def source_scope(self) -> GolRequest:
        if not self.source_object_id.startswith("raw:" + self.tenant_id + "/"):
            raise ValueError("GOL source object unavailable")
        return self


class GolBlockContract(Transport):
    """Deployment selectors convey requirements, never security decisions."""

    state_symbol_key: str | None = Field(default=None, max_length=512)
    derived_symbol_key: str | None = Field(default=None, max_length=512)
    required_symbol_keys: tuple[str, ...] = Field(default=(), max_length=1024)
    allowed_kinds: tuple[GolSymbolKind, ...] = ()


GolOperation = Literal["read", "write", "execute", "transfer", "describe"]


class GolHandoff(Transport):
    """Names the command selected for a permitted broker release."""

    block_id: str = Field(alias="blockId", min_length=1, max_length=128)
    command_id: UUID = Field(alias="commandId")


class GolEffect(Transport):
    """An effect description conveys no authority until the host retains its permit."""

    operation: GolOperation
    target: str = Field(min_length=1, max_length=512)
    destination: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=128)
    instance_id: str | None = Field(
        default=None, alias="instanceId", max_length=512
    )
    proposal: dict[str, JsonValue] | None = None
    receipt: str | None = Field(default=None, max_length=65536, repr=False)
    handoff: GolHandoff | None = None
    read_fields: tuple[str, ...] | None = Field(
        default=None, alias="readFields", min_length=1, max_length=128
    )

    @field_validator("target")
    @classmethod
    def effect_target(cls, value: str) -> str:
        return validate_symbol_key(value)
