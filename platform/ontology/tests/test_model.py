"""Unit tests for ontology identity and canonical wire artifacts."""

from __future__ import annotations

import pytest
from galadril_ontology import (
    Ontology,
    OntologyResource,
    ResourceKind,
)


def test_wire_package_exposes_no_history_or_merge_model() -> None:
    """Prevents callers from treating Python models as semantic authority."""
    import galadril_ontology.model as model

    for name in (
        "ChangeOperation",
        "BaseOntologyArtifact",
        "MaterializedOntology",
        "MergeResult",
        "OntologyBranch",
        "OntologyRevision",
        "OverlaySnapshot",
    ):
        assert not hasattr(model, name)


def test_resource_identity_is_stable_across_display_name_changes() -> None:
    original = OntologyResource(
        resource_id="core.customer",
        kind=ResourceKind.OBJECT_TYPE,
        display_name="Customer",
    )
    renamed = original.model_copy(update={"display_name": "Account Holder"})

    assert renamed.resource_id == original.resource_id
    assert renamed != original


def test_wire_model_defers_duplicate_validation_to_registry() -> None:
    customer = OntologyResource(
        resource_id="core.customer",
        kind=ResourceKind.OBJECT_TYPE,
        display_name="Customer",
    )
    ontology = Ontology(version="v1", resources=(customer, customer))

    assert ontology.resources == (customer, customer)


def test_wire_model_defers_property_semantics_to_registry() -> None:
    property_resource = OntologyResource(
        resource_id="core.customer.email",
        kind=ResourceKind.PROPERTY,
        display_name="Email",
    )

    assert property_resource.owner_id is None
    assert property_resource.value_type is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
