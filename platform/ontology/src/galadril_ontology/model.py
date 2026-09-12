"""Immutable wire models consumed by ontology-aware runtimes."""

from __future__ import annotations

from enum import StrEnum, unique

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
)

from galadril_ontology.errors import OntologyNotFoundError


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


class Ontology(_FrozenModel):
    """An immutable wire projection of a Registry-validated ontology."""

    version: str = Field(min_length=1, max_length=128)
    resources: tuple[OntologyResource, ...] = ()
    _resource_index: dict[str, OntologyResource] = PrivateAttr(
        default_factory=dict
    )

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
