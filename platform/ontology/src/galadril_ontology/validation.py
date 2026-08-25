"""Semantic validation for materialized ontology resources."""

from __future__ import annotations

from collections.abc import Iterable

from galadril_ontology.errors import (
    OntologyValidationError,
    ValidationIssue,
)
from galadril_ontology.model import Ontology, ResourceKind

_SCALAR_VALUE_TYPES = frozenset(
    {
        "boolean",
        "bytes",
        "date",
        "datetime",
        "decimal",
        "float",
        "geopoint",
        "integer",
        "json",
        "string",
        "uuid",
    }
)


def _owner_cycle_issues(ontology: Ontology) -> Iterable[ValidationIssue]:
    owners = {
        resource.resource_id: resource.owner_id
        for resource in ontology.resources
        if resource.owner_id is not None
    }
    for resource_id in owners:
        seen: set[str] = set()
        current: str | None = resource_id
        while current is not None:
            if current in seen:
                yield ValidationIssue(
                    code="owner_cycle",
                    message=f"owner cycle contains {current}",
                    resource_id=resource_id,
                    path=("owner_id",),
                )
                break
            seen.add(current)
            current = owners.get(current)


def validate_ontology(ontology: Ontology) -> None:
    """Validates cross-resource invariants required by runtime consumers."""
    resources = {
        resource.resource_id: resource for resource in ontology.resources
    }
    issues: list[ValidationIssue] = []
    for resource in ontology.resources:
        if resource.owner_id is not None:
            owner = resources.get(resource.owner_id)
            if owner is None:
                issues.append(
                    ValidationIssue(
                        code="dangling_owner",
                        message=(
                            f"{resource.resource_id} references missing owner "
                            f"{resource.owner_id}"
                        ),
                        resource_id=resource.resource_id,
                        path=("owner_id",),
                    )
                )
            elif owner.kind not in {
                ResourceKind.OBJECT_TYPE,
                ResourceKind.EVENT_TYPE,
            }:
                issues.append(
                    ValidationIssue(
                        code="invalid_owner_kind",
                        message=(
                            f"{resource.resource_id} owner {resource.owner_id} "
                            "is not an object or event type"
                        ),
                        resource_id=resource.resource_id,
                        path=("owner_id",),
                    )
                )
        for reference in resource.references:
            if reference not in resources:
                issues.append(
                    ValidationIssue(
                        code="dangling_reference",
                        message=(
                            f"{resource.resource_id} references missing "
                            f"resource {reference}"
                        ),
                        resource_id=resource.resource_id,
                        path=("references",),
                    )
                )
        if resource.kind is ResourceKind.PROPERTY:
            value_type = resource.value_type
            if (
                value_type is not None
                and value_type not in _SCALAR_VALUE_TYPES
                and value_type not in resources
            ):
                issues.append(
                    ValidationIssue(
                        code="invalid_value_type",
                        message=(
                            f"{resource.resource_id} uses unknown value type "
                            f"{value_type}"
                        ),
                        resource_id=resource.resource_id,
                        path=("value_type",),
                    )
                )
    issues.extend(_owner_cycle_issues(ontology))
    if issues:
        raise OntologyValidationError(tuple(issues))
