"""Immutable models for ontology resources and revision history."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum, unique
from typing import Self

import orjson
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    field_validator,
    model_validator,
)

from galadril_ontology.errors import OntologyNotFoundError
from galadril_ontology.identity import (
    normalize_tenant_id,
    validate_branch_name,
    validate_resource_id,
)


class _FrozenModel(BaseModel):
    """Prevents accidental mutation of committed domain values."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@unique
class ResourceKind(StrEnum):
    """Kinds consumed by ontology-aware platform runtimes."""

    OBJECT_TYPE = "object_type"
    EVENT_TYPE = "event_type"
    PROPERTY = "property"
    LINK_TYPE = "link_type"
    ACTION = "action"
    FUNCTION = "function"


class OntologyResource(_FrozenModel):
    """One stable ontology resource with mutable semantic fields."""

    resource_id: str
    kind: ResourceKind
    display_name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4096)
    owner_id: str | None = None
    value_type: str | None = None
    references: tuple[str, ...] = ()
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("resource_id", "owner_id")
    @classmethod
    def _validate_resource_identifier(cls, value: str | None) -> str | None:
        return None if value is None else validate_resource_id(value)

    @field_validator("references")
    @classmethod
    def _validate_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_resource_id(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("resource references must be unique")
        return normalized

    @model_validator(mode="after")
    def _validate_kind_requirements(self) -> Self:
        if self.kind is ResourceKind.PROPERTY:
            if self.owner_id is None:
                raise ValueError("property resources require owner_id")
            if self.value_type is None or not self.value_type:
                raise ValueError("property resources require value_type")
        return self


class Ontology(_FrozenModel):
    """A deterministic, validated-shape collection of ontology resources."""

    version: str = Field(min_length=1, max_length=128)
    resources: tuple[OntologyResource, ...] = ()
    _resource_index: dict[str, OntologyResource] = PrivateAttr(
        default_factory=dict
    )

    @field_validator("resources")
    @classmethod
    def _sort_and_reject_duplicates(
        cls, resources: tuple[OntologyResource, ...]
    ) -> tuple[OntologyResource, ...]:
        ordered = tuple(sorted(resources, key=lambda item: item.resource_id))
        identifiers = tuple(item.resource_id for item in ordered)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate ontology resource_id")
        return ordered

    def model_post_init(self, context: object) -> None:
        """Builds one read-optimized index outside canonical serialized state."""
        self._resource_index.update(
            (resource.resource_id, resource) for resource in self.resources
        )

    def get(self, resource_id: str) -> OntologyResource | None:
        """Returns a resource without exposing a mutable internal index."""
        return self._resource_index.get(resource_id)

    def require(self, resource_id: str) -> OntologyResource:
        """Returns a resource or a domain-specific absence error."""
        resource = self.get(resource_id)
        if resource is None:
            raise OntologyNotFoundError(
                f"ontology resource is unavailable: {resource_id}"
            )
        return resource


def ontology_content_hash(ontology: Ontology) -> str:
    """Computes the canonical content address for an ontology artifact."""
    payload = ontology.model_dump(mode="json")
    canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical).hexdigest()


class BaseOntologyArtifact(_FrozenModel):
    """One globally stored, immutable release of the code-defined ontology."""

    version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ontology: Ontology
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_ontology(cls, ontology: Ontology) -> BaseOntologyArtifact:
        """Packages a code-defined ontology into a reproducible artifact."""
        return cls(
            version=ontology.version,
            content_hash=ontology_content_hash(ontology),
            ontology=ontology,
        )

    @model_validator(mode="after")
    def _validate_artifact(self) -> Self:
        if self.version != self.ontology.version:
            raise ValueError("base artifact and ontology versions differ")
        if self.content_hash != ontology_content_hash(self.ontology):
            raise ValueError(
                "base artifact content hash does not match ontology"
            )
        return self


@unique
class ChangeOperation(StrEnum):
    """Semantic mutations applied to a tenant's sparse overlay."""

    ADD_RESOURCE = "add_resource"
    SET_FIELD = "set_field"
    REMOVE_FIELD = "remove_field"
    REMOVE_RESOURCE = "remove_resource"
    RESTORE_FIELD = "restore_field"
    RESTORE_RESOURCE = "restore_resource"


class OntologyChange(_FrozenModel):
    """One immutable domain-aware change in a revision change set."""

    operation: ChangeOperation
    resource_id: str
    path: tuple[str, ...] = ()
    value: JsonValue = None
    resource: OntologyResource | None = None

    @field_validator("resource_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_resource_id(value)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not segment or "." in segment for segment in value):
            raise ValueError("change paths require nonempty field segments")
        return value

    @model_validator(mode="after")
    def _validate_operation_shape(self) -> Self:
        if self.operation is ChangeOperation.ADD_RESOURCE:
            if self.resource is None:
                raise ValueError("add_resource requires a resource")
            if self.resource.resource_id != self.resource_id:
                raise ValueError("change and resource identifiers differ")
        elif self.resource is not None:
            raise ValueError("only add_resource accepts a resource payload")
        if self.operation in {
            ChangeOperation.SET_FIELD,
            ChangeOperation.REMOVE_FIELD,
            ChangeOperation.RESTORE_FIELD,
        }:
            if not self.path:
                raise ValueError("field operations require a semantic path")
        elif self.path:
            raise ValueError("resource operations cannot contain a field path")
        return self

    @classmethod
    def add_resource(cls, resource: OntologyResource) -> OntologyChange:
        """Constructs a tenant-owned resource addition."""
        return cls(
            operation=ChangeOperation.ADD_RESOURCE,
            resource_id=resource.resource_id,
            resource=resource,
        )

    @classmethod
    def set_field(
        cls,
        resource_id: str,
        path: tuple[str, ...],
        value: JsonValue,
    ) -> OntologyChange:
        """Constructs an explicit field override."""
        return cls(
            operation=ChangeOperation.SET_FIELD,
            resource_id=resource_id,
            path=path,
            value=value,
        )

    @classmethod
    def remove_field(
        cls, resource_id: str, path: tuple[str, ...]
    ) -> OntologyChange:
        """Constructs an explicit field tombstone."""
        return cls(
            operation=ChangeOperation.REMOVE_FIELD,
            resource_id=resource_id,
            path=path,
        )

    @classmethod
    def remove_resource(cls, resource_id: str) -> OntologyChange:
        """Constructs an inherited-resource tombstone or custom deletion."""
        return cls(
            operation=ChangeOperation.REMOVE_RESOURCE,
            resource_id=resource_id,
        )

    @classmethod
    def restore_field(
        cls, resource_id: str, path: tuple[str, ...]
    ) -> OntologyChange:
        """Removes one explicit field override from the tenant overlay."""
        return cls(
            operation=ChangeOperation.RESTORE_FIELD,
            resource_id=resource_id,
            path=path,
        )

    @classmethod
    def restore_resource(cls, resource_id: str) -> OntologyChange:
        """Removes all tenant-owned state for one resource identity."""
        return cls(
            operation=ChangeOperation.RESTORE_RESOURCE,
            resource_id=resource_id,
        )


class FieldOverride(_FrozenModel):
    """Canonical accumulated override for a semantic resource field."""

    path: tuple[str, ...]
    removed: bool = False
    value: JsonValue = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not segment or "." in segment for segment in value):
            raise ValueError("field override requires a valid semantic path")
        return value


class ResourceOverride(_FrozenModel):
    """Canonical sparse override state for one stable resource identity."""

    resource_id: str
    added: OntologyResource | None = None
    removed: bool = False
    fields: tuple[FieldOverride, ...] = ()

    @field_validator("resource_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_resource_id(value)

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        if self.added is not None and self.removed:
            raise ValueError("resource override cannot be added and removed")
        if (
            self.added is not None
            and self.added.resource_id != self.resource_id
        ):
            raise ValueError("override and added resource identifiers differ")
        paths = tuple(field.path for field in self.fields)
        if len(set(paths)) != len(paths):
            raise ValueError("resource override field paths must be unique")
        return self


class OverlaySnapshot(_FrozenModel):
    """Serializable accumulated tenant overlay used by disposable caches."""

    resources: tuple[ResourceOverride, ...] = ()

    @field_validator("resources")
    @classmethod
    def _sort_and_reject_duplicates(
        cls, resources: tuple[ResourceOverride, ...]
    ) -> tuple[ResourceOverride, ...]:
        ordered = tuple(sorted(resources, key=lambda item: item.resource_id))
        identifiers = tuple(item.resource_id for item in ordered)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate resource override")
        return ordered


class OntologyRevision(_FrozenModel):
    """An immutable tenant commit with ordered DAG parents."""

    tenant_id: str
    revision_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    base_version: str
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parents: tuple[str, ...] = ()
    changes: tuple[OntologyChange, ...] = ()
    author: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=4096)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("tenant_id")
    @classmethod
    def _normalize_tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @field_validator("parents")
    @classmethod
    def _validate_parents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 2:
            raise ValueError("ontology revisions support at most two parents")
        if len(set(value)) != len(value):
            raise ValueError("revision parents must be distinct")
        if any(
            len(parent) != 32
            or any(character not in "0123456789abcdef" for character in parent)
            for parent in value
        ):
            raise ValueError("revision parents require lowercase UUID hex")
        return value

    @model_validator(mode="after")
    def _reject_self_parent(self) -> Self:
        if self.revision_id in self.parents:
            raise ValueError("revision cannot be its own parent")
        return self


class OntologyBranch(_FrozenModel):
    """A lightweight tenant-local reference to an immutable revision."""

    tenant_id: str
    name: str
    head_revision_id: str = Field(pattern=r"^[0-9a-f]{32}$")

    @field_validator("tenant_id")
    @classmethod
    def _normalize_tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_branch_name(value)


class MaterializedOntology(_FrozenModel):
    """A validated effective ontology and its reconstructable overlay cache."""

    tenant_id: str
    revision_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    base_version: str
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    overlay: OverlaySnapshot
    ontology: Ontology

    @field_validator("tenant_id")
    @classmethod
    def _normalize_tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)


@unique
class ConflictKind(StrEnum):
    """Structured semantic merge and synchronization conflict categories."""

    FIELD_VALUE = "field_value"
    DELETE_MODIFY = "delete_modify"
    ADD_ADD = "add_add"
    BASE_RESOURCE_REMOVED = "base_resource_removed"
    INVALID_RESULT = "invalid_result"


class ConflictValue(_FrozenModel):
    """Distinguishes an absent value from an explicit JSON null."""

    exists: bool
    value: JsonValue = None


class MergeConflict(_FrozenModel):
    """One explainable, persistable semantic conflict."""

    conflict_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: ConflictKind
    resource_id: str
    path: tuple[str, ...] = ()
    base: ConflictValue
    left: ConflictValue
    right: ConflictValue
    message: str


class MergeResult(_FrozenModel):
    """Result of a merge attempt, including unresolved structured conflicts."""

    merge_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: OntologyRevision | None = None
    conflicts: tuple[MergeConflict, ...] = ()

    @model_validator(mode="after")
    def _validate_result(self) -> Self:
        if self.revision is not None and self.conflicts:
            raise ValueError("a merge cannot commit with unresolved conflicts")
        return self
