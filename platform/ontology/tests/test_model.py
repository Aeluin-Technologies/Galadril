"""Unit tests for ontology identity, validation, and canonical artifacts."""

from __future__ import annotations

import pytest
from galadril_ontology import (
    BaseOntologyArtifact,
    Ontology,
    OntologyResource,
    OntologyValidationError,
    ResourceKind,
    validate_ontology,
)
from pydantic import ValidationError


def test_resource_identity_is_stable_across_display_name_changes() -> None:
    original = OntologyResource(
        resource_id="core.customer",
        kind=ResourceKind.OBJECT_TYPE,
        display_name="Customer",
    )
    renamed = original.model_copy(update={"display_name": "Account Holder"})

    assert renamed.resource_id == original.resource_id
    assert renamed != original


def test_base_hash_is_canonical_and_content_addressed() -> None:
    resource = OntologyResource(
        resource_id="core.customer",
        kind=ResourceKind.OBJECT_TYPE,
        display_name="Customer",
        attributes={"z": 1, "a": 2},
    )
    first = BaseOntologyArtifact.from_ontology(
        Ontology(version="v1", resources=(resource,))
    )
    second = BaseOntologyArtifact.from_ontology(
        Ontology(version="v1", resources=(resource,))
    )

    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64


def test_duplicate_and_dangling_resource_ids_are_rejected() -> None:
    customer = OntologyResource(
        resource_id="core.customer",
        kind=ResourceKind.OBJECT_TYPE,
        display_name="Customer",
    )
    with pytest.raises(ValidationError, match="duplicate"):
        Ontology(version="v1", resources=(customer, customer))

    dangling = Ontology(
        version="v1",
        resources=(
            OntologyResource(
                resource_id="core.customer.email",
                kind=ResourceKind.PROPERTY,
                display_name="Email",
                owner_id="core.missing",
                value_type="string",
            ),
        ),
    )
    with pytest.raises(OntologyValidationError) as error:
        validate_ontology(dangling)
    assert error.value.issues[0].code == "dangling_owner"


def test_property_requires_owner_and_value_type() -> None:
    with pytest.raises(ValidationError):
        OntologyResource(
            resource_id="core.customer.email",
            kind=ResourceKind.PROPERTY,
            display_name="Email",
        )
