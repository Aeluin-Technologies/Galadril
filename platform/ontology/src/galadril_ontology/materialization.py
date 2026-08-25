"""Sparse overlay replay and deterministic effective materialization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import cast

from pydantic import JsonValue, ValidationError

from galadril_ontology.errors import InvalidOntologyChangeError
from galadril_ontology.model import (
    ChangeOperation,
    FieldOverride,
    Ontology,
    OntologyChange,
    OntologyResource,
    OverlaySnapshot,
    ResourceOverride,
)

_MUTABLE_ROOT_FIELDS = frozenset(
    {"display_name", "description", "owner_id", "value_type", "references"}
)


@dataclass(slots=True)
class OverlayAccumulator:
    """Mutable transaction-local representation of a sparse tenant overlay."""

    added: dict[str, OntologyResource] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    fields: dict[str, dict[tuple[str, ...], FieldOverride]] = field(
        default_factory=dict
    )

    @classmethod
    def from_snapshot(cls, snapshot: OverlaySnapshot) -> OverlayAccumulator:
        """Creates an isolated accumulator without mutating cached state."""
        accumulator = cls()
        for resource in snapshot.resources:
            if resource.added is not None:
                accumulator.added[resource.resource_id] = resource.added
            if resource.removed:
                accumulator.removed.add(resource.resource_id)
            if resource.fields:
                accumulator.fields[resource.resource_id] = {
                    override.path: override for override in resource.fields
                }
        return accumulator

    def snapshot(self) -> OverlaySnapshot:
        """Returns a deterministically ordered, serializable cache value."""
        identifiers = set(self.added) | self.removed | set(self.fields)
        resources = tuple(
            ResourceOverride(
                resource_id=resource_id,
                added=self.added.get(resource_id),
                removed=resource_id in self.removed,
                fields=tuple(
                    sorted(
                        self.fields.get(resource_id, {}).values(),
                        key=lambda item: item.path,
                    )
                ),
            )
            for resource_id in sorted(identifiers)
        )
        return OverlaySnapshot(resources=resources)


def _resource_payload(resource: OntologyResource) -> dict[str, object]:
    payload = resource.model_dump(mode="json")
    return cast(dict[str, object], payload)


def _json_mapping(value: object, *, path: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        joined = ".".join(path)
        raise InvalidOntologyChangeError(
            f"semantic path traverses a non-object value: {joined}"
        )
    return cast(dict[str, object], value)


def _validate_mutable_path(path: tuple[str, ...]) -> None:
    root = path[0]
    if root in _MUTABLE_ROOT_FIELDS and len(path) == 1:
        return
    if root == "attributes" and len(path) >= 2:
        return
    raise InvalidOntologyChangeError(
        f"field is immutable or unknown: {'.'.join(path)}"
    )


def _read_path(resource: OntologyResource, path: tuple[str, ...]) -> object:
    _validate_mutable_path(path)
    current: object = _resource_payload(resource)
    for segment in path:
        mapping = _json_mapping(current, path=path)
        if segment not in mapping:
            raise InvalidOntologyChangeError(
                f"field does not exist: {resource.resource_id}.{'.'.join(path)}"
            )
        current = mapping[segment]
    return current


def _patch_resource(
    resource: OntologyResource,
    override: FieldOverride,
) -> OntologyResource:
    _validate_mutable_path(override.path)
    payload = deepcopy(_resource_payload(resource))
    current = payload
    for segment in override.path[:-1]:
        child = current.get(segment)
        if child is None and not override.removed:
            child = {}
            current[segment] = child
        current = _json_mapping(child, path=override.path)
    leaf = override.path[-1]
    if override.removed:
        current.pop(leaf, None)
    else:
        current[leaf] = deepcopy(override.value)
    try:
        return OntologyResource.model_validate(payload)
    except ValidationError as error:
        raise InvalidOntologyChangeError(
            f"invalid value for {resource.resource_id}.{'.'.join(override.path)}"
        ) from error


def materialize_overlay(
    base: Ontology,
    overlay: OverlayAccumulator,
    *,
    effective_version: str,
) -> Ontology:
    """Applies a sparse overlay and hides descendants of suppressed owners."""
    resources = {resource.resource_id: resource for resource in base.resources}
    resources.update(overlay.added)
    suppressed = set(overlay.removed)

    # A hidden owner implicitly hides its owned properties while retaining their
    # overrides so restoration or a later base release remains lossless.
    changed = True
    while changed:
        changed = False
        for resource in resources.values():
            if (
                resource.owner_id in suppressed
                and resource.resource_id not in suppressed
            ):
                suppressed.add(resource.resource_id)
                changed = True
    for resource_id in suppressed:
        resources.pop(resource_id, None)

    for resource_id, field_overrides in overlay.fields.items():
        patched_resource = resources.get(resource_id)
        if patched_resource is None:
            raise InvalidOntologyChangeError(
                f"field override targets unavailable resource: {resource_id}"
            )
        for path in sorted(field_overrides):
            patched_resource = _patch_resource(
                patched_resource, field_overrides[path]
            )
        resources[resource_id] = patched_resource
    return Ontology(
        version=effective_version,
        resources=tuple(resources.values()),
    )


def apply_changes(
    base: Ontology,
    overlay: OverlayAccumulator,
    changes: tuple[OntologyChange, ...],
) -> None:
    """Applies validated semantic operations to transaction-local state."""
    for change in changes:
        effective = materialize_overlay(
            base, overlay, effective_version=base.version
        )
        existing = effective.get(change.resource_id)
        operation = change.operation
        if operation is ChangeOperation.ADD_RESOURCE:
            if existing is not None or change.resource is None:
                raise InvalidOntologyChangeError(
                    f"resource already exists: {change.resource_id}"
                )
            overlay.added[change.resource_id] = change.resource
            overlay.removed.discard(change.resource_id)
            overlay.fields.pop(change.resource_id, None)
        elif operation is ChangeOperation.REMOVE_RESOURCE:
            if existing is None:
                raise InvalidOntologyChangeError(
                    f"resource does not exist: {change.resource_id}"
                )
            if change.resource_id in overlay.added:
                overlay.added.pop(change.resource_id)
                overlay.fields.pop(change.resource_id, None)
            else:
                overlay.removed.add(change.resource_id)
                overlay.fields.pop(change.resource_id, None)
        elif operation is ChangeOperation.RESTORE_RESOURCE:
            overlay.added.pop(change.resource_id, None)
            overlay.removed.discard(change.resource_id)
            overlay.fields.pop(change.resource_id, None)
        elif operation is ChangeOperation.RESTORE_FIELD:
            overrides = overlay.fields.get(change.resource_id)
            if overrides is not None:
                overrides.pop(change.path, None)
                if not overrides:
                    overlay.fields.pop(change.resource_id, None)
        elif operation in {
            ChangeOperation.SET_FIELD,
            ChangeOperation.REMOVE_FIELD,
        }:
            if existing is None:
                raise InvalidOntologyChangeError(
                    f"field change targets unavailable resource: {change.resource_id}"
                )
            _validate_mutable_path(change.path)
            if (
                operation is ChangeOperation.REMOVE_FIELD
                or change.path[0] != "attributes"
            ):
                _read_path(existing, change.path)
            override = FieldOverride(
                path=change.path,
                removed=operation is ChangeOperation.REMOVE_FIELD,
                value=change.value,
            )
            overlay.fields.setdefault(change.resource_id, {})[change.path] = (
                override
            )


def _flatten_attributes(
    value: dict[str, JsonValue],
    prefix: tuple[str, ...],
) -> dict[tuple[str, ...], JsonValue]:
    flattened: dict[tuple[str, ...], JsonValue] = {}
    if not value:
        return flattened
    for key in sorted(value):
        item = value[key]
        path = (*prefix, key)
        if isinstance(item, dict):
            flattened.update(_flatten_attributes(item, path))
        else:
            flattened[path] = item
    return flattened


def flatten_resource(
    resource: OntologyResource,
) -> dict[tuple[str, ...], JsonValue]:
    """Flattens mutable semantic fields for field-granular diff and merge."""
    fields: dict[tuple[str, ...], JsonValue] = {
        ("display_name",): resource.display_name,
        ("description",): resource.description,
        ("owner_id",): resource.owner_id,
        ("value_type",): resource.value_type,
        ("references",): list(resource.references),
    }
    fields.update(_flatten_attributes(resource.attributes, ("attributes",)))
    return fields


def overlay_from_effective(
    base: Ontology, effective: Ontology
) -> OverlayAccumulator:
    """Derives the minimal sparse overlay represented by an effective ontology."""
    base_resources = {item.resource_id: item for item in base.resources}
    effective_resources = {
        item.resource_id: item for item in effective.resources
    }
    missing_base_resources = set(base_resources) - set(effective_resources)
    overlay = OverlayAccumulator()
    for resource_id in sorted(set(base_resources) | set(effective_resources)):
        base_resource = base_resources.get(resource_id)
        effective_resource = effective_resources.get(resource_id)
        if base_resource is None and effective_resource is not None:
            overlay.added[resource_id] = effective_resource
            continue
        if base_resource is not None and effective_resource is None:
            if base_resource.owner_id in missing_base_resources:
                continue
            overlay.removed.add(resource_id)
            continue
        base_resource = cast(OntologyResource, base_resource)
        effective_resource = cast(OntologyResource, effective_resource)
        if base_resource.kind is not effective_resource.kind:
            raise InvalidOntologyChangeError(
                f"resource kind is immutable: {resource_id}"
            )
        base_fields = flatten_resource(base_resource)
        effective_fields = flatten_resource(effective_resource)
        paths = set(base_fields) | set(effective_fields)
        for path in sorted(paths):
            if path not in effective_fields:
                override = FieldOverride(path=path, removed=True)
            elif (
                path not in base_fields
                or effective_fields[path] != base_fields[path]
            ):
                override = FieldOverride(
                    path=path, value=effective_fields[path]
                )
            else:
                continue
            overlay.fields.setdefault(resource_id, {})[path] = override
    return overlay


def changes_between_overlays(
    current: OverlayAccumulator,
    desired: OverlayAccumulator,
) -> tuple[OntologyChange, ...]:
    """Builds semantic operations that transform one sparse overlay into another."""
    current_resources = {
        resource.resource_id: resource
        for resource in current.snapshot().resources
    }
    desired_resources = {
        resource.resource_id: resource
        for resource in desired.snapshot().resources
    }
    changes: list[OntologyChange] = []
    for resource_id in sorted(set(current_resources) | set(desired_resources)):
        before = current_resources.get(resource_id)
        after = desired_resources.get(resource_id)
        if before == after:
            continue
        if before is not None:
            changes.append(OntologyChange.restore_resource(resource_id))
        if after is None:
            continue
        if after.added is not None:
            changes.append(OntologyChange.add_resource(after.added))
        elif after.removed:
            changes.append(OntologyChange.remove_resource(resource_id))
        for field_override in after.fields:
            if field_override.removed:
                changes.append(
                    OntologyChange.remove_field(
                        resource_id, field_override.path
                    )
                )
            else:
                changes.append(
                    OntologyChange.set_field(
                        resource_id,
                        field_override.path,
                        field_override.value,
                    )
                )
    return tuple(changes)
