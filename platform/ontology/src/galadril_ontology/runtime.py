"""Tenant-scoped production ontology publication and runtime slice contracts."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Protocol, Self

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from galadril_ontology.errors import (
    OntologyCompatibilityError,
    OntologyNotFoundError,
    OntologyValidationError,
)
from galadril_ontology.identity import normalize_tenant_id, validate_resource_id
from galadril_ontology.model import MaterializedOntology, Ontology, ResourceKind
from galadril_ontology.validation import validate_ontology

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


class OntologySliceSelector(_RuntimeModel):
    """Selects the minimum ontology surface needed by one pipeline block."""

    resource_ids: tuple[str, ...] = ()
    kinds: tuple[ResourceKind, ...] = ()
    include_dependencies: bool = True

    @field_validator("resource_ids")
    @classmethod
    def _validate_resources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_resource_id(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("slice resource identifiers must be unique")
        return normalized

    @field_validator("kinds")
    @classmethod
    def _validate_kinds(
        cls, values: tuple[ResourceKind, ...]
    ) -> tuple[ResourceKind, ...]:
        if len(set(values)) != len(values):
            raise ValueError("slice resource kinds must be unique")
        return values

    @model_validator(mode="after")
    def _require_constraint(self) -> Self:
        if not self.resource_ids and not self.kinds:
            raise ValueError("ontology slice selector cannot be empty")
        return self


class PipelineOntologyBinding(_RuntimeModel):
    """Maps one tenant pipeline block to one tenant-owned ontology."""

    tenant_id: str
    pipeline_id: str
    block_id: str
    ontology_id: str
    selector: OntologySliceSelector
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("tenant_id")
    @classmethod
    def _normalize_tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @field_validator("pipeline_id", "block_id", "ontology_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_runtime_id(value)


class PublishedOntology(_RuntimeModel):
    """The latest production pointer and metadata for one tenant ontology."""

    tenant_id: str
    ontology_id: str
    publication_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    materialization: MaterializedOntology
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("tenant_id")
    @classmethod
    def _normalize_tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @field_validator("ontology_id")
    @classmethod
    def _validate_ontology_id(cls, value: str) -> str:
        return _validate_runtime_id(value)

    @model_validator(mode="after")
    def _validate_scope(self) -> Self:
        if self.materialization.tenant_id != self.tenant_id:
            raise ValueError("publication and materialization tenants differ")
        return self


class OntologySliceMetadata(_RuntimeModel):
    """Provenance required to pin and explain one runtime ontology slice."""

    tenant_id: str
    ontology_id: str
    publication_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    base_version: str
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    binding_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    published_at: datetime


class OntologySlice(_RuntimeModel):
    """A validated block-local ontology view loaded from PostgreSQL."""

    metadata: OntologySliceMetadata
    ontology: Ontology


class OntologySliceRequest(_RuntimeModel):
    """Identifies one block execution within a tenant pipeline."""

    tenant_id: str
    pipeline_id: str
    block_id: str

    @field_validator("tenant_id")
    @classmethod
    def _normalize_tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @field_validator("pipeline_id", "block_id")
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


class InMemoryOntologyRuntimeStore:
    """Deterministic runtime store for tests and non-production composition."""

    __slots__ = ("_bindings", "_load_count", "_publications")

    def __init__(self) -> None:
        self._publications: dict[tuple[str, str], PublishedOntology] = {}
        self._bindings: dict[tuple[str, str, str], PipelineOntologyBinding] = {}
        self._load_count = 0

    @property
    def publication_count(self) -> int:
        return len(self._publications)

    @property
    def load_count(self) -> int:
        return self._load_count

    async def publish(self, publication: PublishedOntology) -> None:
        """Atomically replaces the production pointer for one tenant ontology."""
        self._publications[(publication.tenant_id, publication.ontology_id)] = (
            publication
        )

    async def bind(self, binding: PipelineOntologyBinding) -> None:
        """Creates or replaces one block binding within a tenant namespace."""
        publication_key = (binding.tenant_id, binding.ontology_id)
        if publication_key not in self._publications:
            raise OntologyNotFoundError("tenant ontology is not published")
        self._bindings[
            (binding.tenant_id, binding.pipeline_id, binding.block_id)
        ] = binding

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice:
        """Resolves current state on every call to model database semantics."""
        self._load_count += 1
        binding = self._bindings.get(
            (request.tenant_id, request.pipeline_id, request.block_id)
        )
        if binding is None:
            raise OntologyNotFoundError(
                "pipeline block ontology binding is unavailable"
            )
        publication = self._publications.get(
            (request.tenant_id, binding.ontology_id)
        )
        if publication is None:
            raise OntologyNotFoundError("tenant ontology is not published")
        ontology = _slice_ontology(
            publication.materialization.ontology, binding.selector
        )
        materialization = publication.materialization
        return OntologySlice(
            metadata=OntologySliceMetadata(
                tenant_id=request.tenant_id,
                ontology_id=publication.ontology_id,
                publication_id=publication.publication_id,
                revision_id=materialization.revision_id,
                base_version=materialization.base_version,
                base_hash=materialization.base_hash,
                effective_hash=materialization.effective_hash,
                publication_metadata=publication.metadata,
                binding_metadata=binding.metadata,
                published_at=publication.published_at,
            ),
            ontology=ontology,
        )


def _slice_ontology(
    ontology: Ontology, selector: OntologySliceSelector
) -> Ontology:
    resources = {
        resource.resource_id: resource for resource in ontology.resources
    }
    selected = {
        resource.resource_id
        for resource in ontology.resources
        if resource.resource_id in selector.resource_ids
        or resource.kind in selector.kinds
    }
    if selector.include_dependencies:
        pending = list(selected)
        while pending:
            resource_id = pending.pop()
            resource = resources.get(resource_id)
            if resource is None:
                continue
            dependencies = (*resource.references, resource.owner_id)
            for dependency in dependencies:
                if dependency is not None and dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
    return Ontology(
        version=ontology.version,
        resources=tuple(
            resource
            for resource in ontology.resources
            if resource.resource_id in selected
        ),
    )


_ACTIVE_ONTOLOGY_SLICE: ContextVar[OntologySlice | None] = ContextVar(
    "active_ontology_slice", default=None
)


def active_ontology_slice() -> OntologySlice | None:
    """Returns the ontology view bound to the current asynchronous task."""
    return _ACTIVE_ONTOLOGY_SLICE.get()


class OntologyRuntimeManager:
    """Loads and validates PostgreSQL runtime state for every block execution."""

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
        try:
            validate_ontology(ontology_slice.ontology)
        except OntologyValidationError as error:
            raise OntologyCompatibilityError(
                "ontology slice has invalid dependency closure"
            ) from error
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
