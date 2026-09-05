"""Exhaustive ontology domain invariant and state-transition tests."""

from __future__ import annotations

import pytest
from galadril_ontology.errors import (
    InvalidOntologyChangeError,
    OntologyNotFoundError,
    OntologyValidationError,
)
from galadril_ontology.identity import (
    normalize_tenant_id,
    require_same_tenant,
    validate_branch_name,
    validate_resource_id,
)
from galadril_ontology.materialization import (
    OverlayAccumulator,
    apply_changes,
    changes_between_overlays,
    flatten_resource,
    materialize_overlay,
    overlay_from_effective,
)
from galadril_ontology.merge import (
    _set_nested,
    _Value,
    semantic_diff,
    three_way_merge,
)
from galadril_ontology.model import (
    BaseOntologyArtifact,
    ChangeOperation,
    ConflictKind,
    ConflictValue,
    FieldOverride,
    MergeConflict,
    MergeResult,
    Ontology,
    OntologyChange,
    OntologyResource,
    OntologyRevision,
    OverlaySnapshot,
    ResourceKind,
    ResourceOverride,
    ontology_content_hash,
)
from galadril_ontology.validation import validate_ontology
from pydantic import ValidationError


def _object(
    *,
    resource_id: str = "core.customer",
    kind: ResourceKind = ResourceKind.OBJECT_TYPE,
    description: str = "base",
    attributes: dict[str, object] | None = None,
    references: tuple[str, ...] = (),
) -> OntologyResource:
    return OntologyResource.model_validate(
        {
            "resource_id": resource_id,
            "kind": kind,
            "display_name": "Resource",
            "description": description,
            "attributes": attributes or {},
            "references": references,
        }
    )


def _property(
    *,
    resource_id: str = "core.customer.email",
    owner_id: str = "core.customer",
    value_type: str = "string",
    attributes: dict[str, object] | None = None,
) -> OntologyResource:
    return OntologyResource.model_validate(
        {
            "resource_id": resource_id,
            "kind": ResourceKind.PROPERTY,
            "display_name": "Property",
            "owner_id": owner_id,
            "value_type": value_type,
            "attributes": attributes or {},
        }
    )


def test_identity_validation_covers_normalization_and_rejection() -> None:
    assert normalize_tenant_id(" tenant: tenant-a ") == "tenant-a"
    assert require_same_tenant("tenant:tenant-a", "tenant-a") == "tenant-a"
    assert validate_resource_id("core.customer") == "core.customer"
    assert validate_branch_name("feature/customer") == "feature/customer"
    for value in (None, "", "x" * 129, "bad tenant"):
        with pytest.raises(ValueError):
            normalize_tenant_id(value)
    with pytest.raises(ValueError, match="tenant mismatch"):
        require_same_tenant("tenant-a", "tenant-b")
    with pytest.raises(ValueError, match="resource_id"):
        validate_resource_id("Customer")
    for name in ("bad..branch", "bad branch"):
        with pytest.raises(ValueError, match="branch name"):
            validate_branch_name(name)


def test_model_rejects_every_invalid_immutable_shape() -> None:
    customer = _object()
    with pytest.raises(ValidationError, match="references must be unique"):
        _object(references=("core.customer", "core.customer"))
    for payload in (
        {
            "resource_id": "core.customer.email",
            "kind": "property",
            "display_name": "Email",
            "value_type": "string",
        },
        {
            "resource_id": "core.customer.email",
            "kind": "property",
            "display_name": "Email",
            "owner_id": "core.customer",
        },
    ):
        with pytest.raises(ValidationError):
            OntologyResource.model_validate(payload)
    with pytest.raises(OntologyNotFoundError):
        Ontology(version="v1", resources=(customer,)).require("core.missing")
    with pytest.raises(ValidationError, match="versions differ"):
        BaseOntologyArtifact(
            version="v2",
            content_hash=ontology_content_hash(Ontology(version="v1")),
            ontology=Ontology(version="v1"),
        )
    with pytest.raises(ValidationError, match="content hash"):
        BaseOntologyArtifact(
            version="v1", content_hash="0" * 64, ontology=Ontology(version="v1")
        )

    invalid_changes: tuple[dict[str, object], ...] = (
        {
            "operation": "add_resource",
            "resource_id": "core.customer",
        },
        {
            "operation": "add_resource",
            "resource_id": "core.invoice",
            "resource": customer,
        },
        {
            "operation": "remove_resource",
            "resource_id": "core.customer",
            "resource": customer,
        },
        {
            "operation": "set_field",
            "resource_id": "core.customer",
        },
        {
            "operation": "remove_resource",
            "resource_id": "core.customer",
            "path": ("description",),
        },
        {
            "operation": "set_field",
            "resource_id": "core.customer",
            "path": ("bad.segment",),
        },
    )
    for invalid_change in invalid_changes:
        with pytest.raises(ValidationError):
            OntologyChange.model_validate(invalid_change)
    assert (
        OntologyChange.remove_field("core.customer", ("description",)).operation
        is ChangeOperation.REMOVE_FIELD
    )


def test_override_revision_and_merge_models_reject_invalid_state() -> None:
    customer = _object()
    field = FieldOverride(path=("description",), value="tenant")
    with pytest.raises(ValidationError):
        FieldOverride(path=())
    for payload in (
        {
            "resource_id": "core.customer",
            "added": customer,
            "removed": True,
        },
        {"resource_id": "core.invoice", "added": customer},
        {"resource_id": "core.customer", "fields": (field, field)},
    ):
        with pytest.raises(ValidationError):
            ResourceOverride.model_validate(payload)
    with pytest.raises(ValidationError, match="duplicate resource override"):
        OverlaySnapshot(
            resources=(
                ResourceOverride(resource_id="core.customer"),
                ResourceOverride(resource_id="core.customer"),
            )
        )

    revision_payload = {
        "tenant_id": "tenant-a",
        "revision_id": "1" * 32,
        "base_version": "v1",
        "base_hash": "a" * 64,
        "author": "test",
        "message": "test",
    }
    for parents in (
        ("2" * 32, "3" * 32, "4" * 32),
        ("2" * 32, "2" * 32),
        ("INVALID",),
        ("1" * 32,),
    ):
        with pytest.raises(ValidationError):
            OntologyRevision.model_validate(
                {**revision_payload, "parents": parents}
            )
    revision = OntologyRevision.model_validate(revision_payload)
    conflict = MergeConflict(
        conflict_id="2" * 32,
        kind=ConflictKind.FIELD_VALUE,
        resource_id="core.customer",
        base=ConflictValue(exists=True),
        left=ConflictValue(exists=True),
        right=ConflictValue(exists=True),
        message="conflict",
    )
    with pytest.raises(ValidationError, match="cannot commit"):
        MergeResult(merge_id="3" * 32, revision=revision, conflicts=(conflict,))


def test_materialization_rejects_invalid_paths_and_values() -> None:
    customer = _object(attributes={"nested": "scalar", "remove": "value"})
    base = Ontology(version="v1", resources=(customer,))
    overlay = OverlayAccumulator()
    for change in (
        OntologyChange.set_field("core.customer", ("kind",), "event_type"),
        OntologyChange.remove_field("core.customer", ("attributes", "missing")),
        OntologyChange.set_field(
            "core.customer", ("attributes", "nested", "leaf"), "value"
        ),
        OntologyChange.set_field("core.missing", ("description",), "value"),
    ):
        with pytest.raises(InvalidOntologyChangeError):
            candidate = OverlayAccumulator()
            apply_changes(base, candidate, (change,))
            materialize_overlay(base, candidate, effective_version="v1")
    with pytest.raises(InvalidOntologyChangeError, match="invalid value"):
        apply_changes(
            base,
            overlay,
            (OntologyChange.set_field("core.customer", ("display_name",), ""),),
        )
        materialize_overlay(base, overlay, effective_version="v1")

    unavailable = OverlayAccumulator()
    unavailable.fields["core.missing"] = {
        ("description",): FieldOverride(path=("description",), value="x")
    }
    with pytest.raises(InvalidOntologyChangeError, match="unavailable"):
        materialize_overlay(base, unavailable, effective_version="v1")


def test_materialization_covers_add_remove_restore_and_nested_fields() -> None:
    customer = _object(attributes={"remove": "value"})
    base = Ontology(version="v1", resources=(customer,))
    custom = _object(resource_id="tenant.contract")
    overlay = OverlayAccumulator()
    with pytest.raises(InvalidOntologyChangeError, match="already exists"):
        apply_changes(base, overlay, (OntologyChange.add_resource(customer),))
    with pytest.raises(InvalidOntologyChangeError, match="does not exist"):
        apply_changes(
            base, overlay, (OntologyChange.remove_resource("core.missing"),)
        )

    apply_changes(base, overlay, (OntologyChange.add_resource(custom),))
    apply_changes(
        base, overlay, (OntologyChange.remove_resource(custom.resource_id),)
    )
    apply_changes(
        base,
        overlay,
        (
            OntologyChange.set_field(
                "core.customer", ("attributes", "nested", "leaf"), "value"
            ),
            OntologyChange.remove_field(
                "core.customer", ("attributes", "remove")
            ),
        ),
    )
    effective = materialize_overlay(base, overlay, effective_version="v1")
    assert effective.require("core.customer").attributes == {
        "nested": {"leaf": "value"}
    }
    apply_changes(
        base,
        overlay,
        (
            OntologyChange.restore_field(
                "core.customer", ("attributes", "nested", "leaf")
            ),
            OntologyChange.restore_field(
                "core.customer", ("attributes", "remove")
            ),
            OntologyChange.restore_resource("core.customer"),
        ),
    )
    assert materialize_overlay(base, overlay, effective_version="v1") == base
    assert flatten_resource(customer)[("attributes", "remove")] == "value"


def test_overlay_derivation_and_change_compilation_cover_all_semantics() -> (
    None
):
    customer = _object(attributes={"old": "value"})
    email = _property()
    base = Ontology(version="v1", resources=(customer, email))
    changed_customer = customer.model_copy(
        update={"description": "changed", "attributes": {"new": "value"}}
    )
    custom = _object(resource_id="tenant.contract")
    effective = Ontology(version="v1", resources=(changed_customer, custom))
    overlay = overlay_from_effective(base, effective)
    changes = changes_between_overlays(OverlayAccumulator(), overlay)
    assert {change.operation for change in changes} == {
        ChangeOperation.ADD_RESOURCE,
        ChangeOperation.SET_FIELD,
        ChangeOperation.REMOVE_FIELD,
        ChangeOperation.REMOVE_RESOURCE,
    }
    assert changes_between_overlays(overlay, overlay) == ()
    assert any(
        change.operation is ChangeOperation.RESTORE_RESOURCE
        for change in changes_between_overlays(overlay, OverlayAccumulator())
    )
    wrong_kind = Ontology(
        version="v1",
        resources=(
            changed_customer.model_copy(
                update={"kind": ResourceKind.EVENT_TYPE}
            ),
            email,
        ),
    )
    with pytest.raises(InvalidOntologyChangeError, match="kind is immutable"):
        overlay_from_effective(base, wrong_kind)


def test_semantic_merge_and_diff_cover_independent_and_conflicting_shapes() -> (
    None
):
    base_resource = _object(attributes={"nested": {"old": "value"}})
    base = Ontology(version="v1", resources=(base_resource,))
    left_resource = base_resource.model_copy(
        update={"description": "left", "attributes": {}}
    )
    right_resource = base_resource.model_copy(update={"display_name": "Right"})
    merged, conflicts = three_way_merge(
        base,
        Ontology(version="v1", resources=(left_resource,)),
        Ontology(version="v1", resources=(right_resource,)),
        result_version="v1",
    )
    assert not conflicts
    assert merged is not None
    assert merged.require("core.customer").description == "left"
    assert merged.require("core.customer").display_name == "Right"

    different_kind = base_resource.model_copy(
        update={"kind": ResourceKind.EVENT_TYPE}
    )
    merged, conflicts = three_way_merge(
        base,
        Ontology(version="v1", resources=(different_kind,)),
        Ontology(version="v1", resources=(right_resource,)),
        result_version="v1",
    )
    assert merged is None
    assert conflicts[0].path == ("kind",)

    added_left = _object(resource_id="tenant.contract", description="left")
    added_right = added_left.model_copy(update={"description": "right"})
    merged, conflicts = three_way_merge(
        Ontology(version="v1"),
        Ontology(version="v1", resources=(added_left,)),
        Ontology(version="v1", resources=(added_right,)),
        result_version="v1",
    )
    assert merged is None
    assert conflicts[0].kind is ConflictKind.ADD_ADD

    after = Ontology(
        version="v2",
        resources=(right_resource, _object(resource_id="tenant.contract")),
    )
    operations = {change.operation for change in semantic_diff(base, after)}
    assert ChangeOperation.ADD_RESOURCE in operations
    assert ChangeOperation.SET_FIELD in operations
    reverse = {change.operation for change in semantic_diff(after, base)}
    assert ChangeOperation.REMOVE_RESOURCE in reverse
    removed_attribute = Ontology(
        version="v1",
        resources=(base_resource.model_copy(update={"attributes": {}}),),
    )
    assert any(
        change.operation is ChangeOperation.REMOVE_FIELD
        for change in semantic_diff(base, removed_attribute)
    )
    payload: dict[str, object] = {"attributes": {"nested": "scalar"}}
    _set_nested(
        payload, ("attributes", "nested", "leaf"), _Value(True, "value")
    )
    _set_nested(payload, ("attributes", "nested", "leaf"), _Value(False))
    assert payload == {"attributes": {"nested": {}}}


def test_validation_reports_every_cross_resource_invariant() -> None:
    invalid_owner = _property(
        owner_id="core.missing", value_type="unknown.type"
    )
    dangling_reference = _object(references=("core.missing",))
    first = _property(
        resource_id="core.first.value", owner_id="core.second.value"
    )
    second = _property(
        resource_id="core.second.value", owner_id="core.first.value"
    )
    ontology = Ontology(
        version="v1",
        resources=(invalid_owner, dangling_reference, first, second),
    )
    with pytest.raises(OntologyValidationError) as captured:
        validate_ontology(ontology)
    codes = {issue.code for issue in captured.value.issues}
    assert codes == {
        "dangling_owner",
        "dangling_reference",
        "invalid_value_type",
        "invalid_owner_kind",
        "owner_cycle",
    }
