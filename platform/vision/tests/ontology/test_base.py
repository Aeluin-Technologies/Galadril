"""Tests for Vision's code-defined platform ontology."""

from __future__ import annotations

import pytest
from galadril_ontology import InMemoryOntologyRepository, ResourceKind
from galadril_vision.common.types import EntityType, EventType
from galadril_vision.ontology import (
    initialize_vision_ontology,
    vision_base_artifact,
)


def test_vision_base_ontology_is_derived_from_code_definitions() -> None:
    artifact = vision_base_artifact()
    ontology = artifact.ontology

    for entity_type in EntityType:
        resource = ontology.require(f"core.entity.{entity_type.name.lower()}")
        assert resource.kind is ResourceKind.OBJECT_TYPE
        assert resource.display_name == entity_type.value

    for event_type in EventType:
        resource = ontology.require(f"core.event.{event_type.name.lower()}")
        assert resource.kind is ResourceKind.EVENT_TYPE
        assert resource.display_name == event_type.value


def test_vision_base_artifact_is_stable_within_release() -> None:
    first = vision_base_artifact()
    second = vision_base_artifact()

    assert first is second
    assert first.version.startswith("vision-")


@pytest.mark.asyncio
async def test_vision_initialization_registers_the_code_defined_base() -> None:
    repository = InMemoryOntologyRepository()

    service = await initialize_vision_ontology(repository)
    branch = await service.initialize_tenant("tenant-a")
    effective = await service.materialize("tenant-a", branch.head_revision_id)

    assert effective.base_hash == vision_base_artifact().content_hash
    assert effective.ontology == vision_base_artifact().ontology
