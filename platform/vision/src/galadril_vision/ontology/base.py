"""Code-defined platform ontology packaged for tenant inheritance."""

from __future__ import annotations

from functools import lru_cache

from galadril_ontology import (
    BaseOntologyArtifact,
    Ontology,
    OntologyRepository,
    OntologyResource,
    OntologyService,
    ResourceKind,
)

from galadril_vision.common.types import EntityType, EventType

BASE_ONTOLOGY_VERSION = "vision-0.1.0"


def _entity_resources() -> tuple[OntologyResource, ...]:
    return tuple(
        OntologyResource(
            resource_id=f"core.entity.{entity_type.name.lower()}",
            kind=ResourceKind.OBJECT_TYPE,
            display_name=entity_type.value,
            description=(
                "Code-defined Vision entity type used by the ESKG runtime."
            ),
            attributes={"vision_enum": entity_type.name},
        )
        for entity_type in EntityType
    )


def _event_resources() -> tuple[OntologyResource, ...]:
    return tuple(
        OntologyResource(
            resource_id=f"core.event.{event_type.name.lower()}",
            kind=ResourceKind.EVENT_TYPE,
            display_name=event_type.value,
            description=(
                "Code-defined Vision event type used by ingestion and ESKG."
            ),
            attributes={"vision_enum": event_type.name},
        )
        for event_type in EventType
    )


@lru_cache(maxsize=1)
def vision_base_artifact() -> BaseOntologyArtifact:
    """Returns the immutable base artifact compiled into this Vision release."""
    ontology = Ontology(
        version=BASE_ONTOLOGY_VERSION,
        resources=(*_entity_resources(), *_event_resources()),
    )
    return BaseOntologyArtifact.from_ontology(ontology)


async def initialize_vision_ontology(
    repository: OntologyRepository,
) -> OntologyService:
    """Registers this release's shared base and returns the tenant service."""
    await repository.register_base(vision_base_artifact())
    return OntologyService(repository)
