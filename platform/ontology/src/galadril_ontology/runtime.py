"""Tenant-scoped runtime contracts for Registry-derived ontology slices."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Protocol

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
)

from galadril_ontology.errors import OntologyCompatibilityError
from galadril_ontology.identity import normalize_tenant_id, validate_resource_id
from galadril_ontology.model import Ontology, ResourceKind

_RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
logger = structlog.get_logger(__name__)


def _validate_runtime_id(value: str) -> str:
    normalized = value.strip()
    if _RUNTIME_ID_RE.fullmatch(normalized) is None:
        raise ValueError("runtime identifier contains unsupported characters")
    return normalized


class _RuntimeModel(BaseModel):
    """Keeps runtime contracts immutable across asynchronous execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OntologySliceMetadata(_RuntimeModel):
    """Provenance required to pin and explain one runtime ontology slice."""

    tenant_id: str
    ontology_id: str
    publication_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    revision_id: str = Field(pattern=r"^[A-Za-z0-9_-]{20,128}$")
    base_version: str
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    binding_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    published_at: datetime


class OntologySlice(_RuntimeModel):
    """A validated block-local ontology view loaded from the authority."""

    metadata: OntologySliceMetadata
    ontology: Ontology


class OntologySliceRequest(_RuntimeModel):
    """Identifies one block execution within a tenant pipeline."""

    tenant_id: str
    pipeline_id: str
    pipeline_revision_id: str
    block_id: str

    @field_validator("tenant_id")
    @classmethod
    def _normalize_tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @field_validator("pipeline_id", "pipeline_revision_id", "block_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_runtime_id(value)


class BlockOntologyContract(_RuntimeModel):
    """Declares the semantic surface accepted by a processing block."""

    required_resource_ids: tuple[str, ...] = ()
    allowed_kinds: tuple[ResourceKind, ...] = ()

    @field_validator("required_resource_ids")
    @classmethod
    def _validate_resources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_resource_id(value) for value in values)


class OntologyRuntimeStore(Protocol):
    """Loads runtime state from the authoritative persistence boundary."""

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice: ...


_ACTIVE_ONTOLOGY_SLICE: ContextVar[OntologySlice | None] = ContextVar(
    "active_ontology_slice", default=None
)


def active_ontology_slice() -> OntologySlice | None:
    """Returns the ontology view bound to the current asynchronous task."""
    return _ACTIVE_ONTOLOGY_SLICE.get()


class OntologyRuntimeManager:
    """Loads and validates authoritative state for every block execution."""

    __slots__ = ("_store",)

    def __init__(self, store: OntologyRuntimeStore) -> None:
        self._store = store

    async def resolve(
        self,
        request: OntologySliceRequest,
        contract: BlockOntologyContract | None = None,
    ) -> OntologySlice:
        """Loads fresh production state and enforces the target block contract."""
        ontology_slice = await self._store.load_runtime_slice(request)
        if ontology_slice.metadata.tenant_id != request.tenant_id:
            raise OntologyCompatibilityError(
                "runtime store returned another tenant"
            )
        effective_contract = contract or BlockOntologyContract()
        for resource_id in effective_contract.required_resource_ids:
            if ontology_slice.ontology.get(resource_id) is None:
                raise OntologyCompatibilityError(
                    f"required resource is unavailable: {resource_id}"
                )
        if effective_contract.allowed_kinds:
            allowed = set(effective_contract.allowed_kinds)
            incompatible = tuple(
                resource.resource_id
                for resource in ontology_slice.ontology.resources
                if resource.kind not in allowed
            )
            if incompatible:
                raise OntologyCompatibilityError(
                    "resource kind is incompatible with block: "
                    + ", ".join(incompatible)
                )
        logger.info(
            "ontology_runtime_slice_resolved",
            tenant_id=request.tenant_id,
            ontology_id=ontology_slice.metadata.ontology_id,
            pipeline_id=request.pipeline_id,
            block_id=request.block_id,
            revision_id=ontology_slice.metadata.revision_id,
            resource_count=len(ontology_slice.ontology.resources),
        )
        return ontology_slice

    @asynccontextmanager
    async def bind(
        self,
        request: OntologySliceRequest,
        contract: BlockOntologyContract | None = None,
    ) -> AsyncIterator[OntologySlice]:
        """Binds one validated slice without leaking it across concurrent tasks."""
        ontology_slice = await self.resolve(request, contract)
        token = _ACTIVE_ONTOLOGY_SLICE.set(ontology_slice)
        try:
            yield ontology_slice
        finally:
            _ACTIVE_ONTOLOGY_SLICE.reset(token)
