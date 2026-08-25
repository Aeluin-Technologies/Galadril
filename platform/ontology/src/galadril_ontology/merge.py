"""Domain-aware ontology diff and semantic three-way merge."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from pydantic import JsonValue

from galadril_ontology.materialization import flatten_resource
from galadril_ontology.model import (
    ConflictKind,
    ConflictValue,
    MergeConflict,
    Ontology,
    OntologyChange,
    OntologyResource,
)


@dataclass(frozen=True, slots=True)
class _Value:
    exists: bool
    value: JsonValue = None

    def public(self) -> ConflictValue:
        return ConflictValue(exists=self.exists, value=self.value)


_MISSING = _Value(exists=False)


def _state(
    fields: dict[tuple[str, ...], JsonValue], path: tuple[str, ...]
) -> _Value:
    if path not in fields:
        return _MISSING
    return _Value(exists=True, value=fields[path])


def _set_nested(
    payload: dict[str, object], path: tuple[str, ...], state: _Value
) -> None:
    current = payload
    for segment in path[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            child = {}
            current[segment] = child
        current = cast(dict[str, object], child)
    leaf = path[-1]
    if state.exists:
        current[leaf] = deepcopy(state.value)
    else:
        current.pop(leaf, None)


def _conflict(
    kind: ConflictKind,
    resource_id: str,
    path: tuple[str, ...],
    base: _Value,
    left: _Value,
    right: _Value,
    message: str,
) -> MergeConflict:
    return MergeConflict(
        conflict_id=uuid4().hex,
        kind=kind,
        resource_id=resource_id,
        path=path,
        base=base.public(),
        left=left.public(),
        right=right.public(),
        message=message,
    )


def _resource_state(resource: OntologyResource | None) -> _Value:
    if resource is None:
        return _MISSING
    payload = cast(JsonValue, resource.model_dump(mode="json"))
    return _Value(exists=True, value=payload)


def _merge_resource_fields(
    resource_id: str,
    base: OntologyResource,
    left: OntologyResource,
    right: OntologyResource,
) -> tuple[OntologyResource | None, tuple[MergeConflict, ...]]:
    if left.kind is not base.kind or right.kind is not base.kind:
        conflict = _conflict(
            ConflictKind.FIELD_VALUE,
            resource_id,
            ("kind",),
            _Value(True, base.kind.value),
            _Value(True, left.kind.value),
            _Value(True, right.kind.value),
            "resource kind changed incompatibly",
        )
        return None, (conflict,)
    base_fields = flatten_resource(base)
    left_fields = flatten_resource(left)
    right_fields = flatten_resource(right)
    selected: dict[tuple[str, ...], _Value] = {}
    conflicts: list[MergeConflict] = []
    for path in sorted(set(base_fields) | set(left_fields) | set(right_fields)):
        base_value = _state(base_fields, path)
        left_value = _state(left_fields, path)
        right_value = _state(right_fields, path)
        if left_value == right_value:
            selected[path] = left_value
        elif left_value == base_value:
            selected[path] = right_value
        elif right_value == base_value:
            selected[path] = left_value
        else:
            conflicts.append(
                _conflict(
                    ConflictKind.FIELD_VALUE,
                    resource_id,
                    path,
                    base_value,
                    left_value,
                    right_value,
                    "both branches changed the same semantic field",
                )
            )
    if conflicts:
        return None, tuple(conflicts)
    payload = cast(dict[str, object], base.model_dump(mode="json"))
    for path, value in selected.items():
        _set_nested(payload, path, value)
    return OntologyResource.model_validate(payload), ()


def three_way_merge(
    base: Ontology,
    left: Ontology,
    right: Ontology,
    *,
    result_version: str,
) -> tuple[Ontology | None, tuple[MergeConflict, ...]]:
    """Merges independent semantic fields and reports unsafe choices."""
    base_resources = {item.resource_id: item for item in base.resources}
    left_resources = {item.resource_id: item for item in left.resources}
    right_resources = {item.resource_id: item for item in right.resources}
    merged: dict[str, OntologyResource] = {}
    conflicts: list[MergeConflict] = []
    identifiers = (
        set(base_resources) | set(left_resources) | set(right_resources)
    )
    for resource_id in sorted(identifiers):
        base_resource = base_resources.get(resource_id)
        left_resource = left_resources.get(resource_id)
        right_resource = right_resources.get(resource_id)
        if left_resource == right_resource:
            if left_resource is not None:
                merged[resource_id] = left_resource
            continue
        if left_resource == base_resource:
            if right_resource is not None:
                merged[resource_id] = right_resource
            continue
        if right_resource == base_resource:
            if left_resource is not None:
                merged[resource_id] = left_resource
            continue
        if base_resource is None:
            conflicts.append(
                _conflict(
                    ConflictKind.ADD_ADD,
                    resource_id,
                    (),
                    _MISSING,
                    _resource_state(left_resource),
                    _resource_state(right_resource),
                    "both branches added different resources with one stable ID",
                )
            )
            continue
        if left_resource is None or right_resource is None:
            conflicts.append(
                _conflict(
                    ConflictKind.DELETE_MODIFY,
                    resource_id,
                    (),
                    _resource_state(base_resource),
                    _resource_state(left_resource),
                    _resource_state(right_resource),
                    "one branch deleted a resource modified by the other",
                )
            )
            continue
        resource, field_conflicts = _merge_resource_fields(
            resource_id,
            base_resource,
            left_resource,
            right_resource,
        )
        conflicts.extend(field_conflicts)
        if resource is not None:
            merged[resource_id] = resource
    if conflicts:
        return None, tuple(conflicts)
    return Ontology(
        version=result_version, resources=tuple(merged.values())
    ), ()


def semantic_diff(
    before: Ontology, after: Ontology
) -> tuple[OntologyChange, ...]:
    """Returns field-granular changes between two effective ontologies."""
    before_resources = {item.resource_id: item for item in before.resources}
    after_resources = {item.resource_id: item for item in after.resources}
    changes: list[OntologyChange] = []
    for resource_id in sorted(set(before_resources) | set(after_resources)):
        old = before_resources.get(resource_id)
        new = after_resources.get(resource_id)
        if old is None and new is not None:
            changes.append(OntologyChange.add_resource(new))
            continue
        if old is not None and new is None:
            changes.append(OntologyChange.remove_resource(resource_id))
            continue
        if old is None or new is None or old == new:
            continue
        old_fields = flatten_resource(old)
        new_fields = flatten_resource(new)
        for path in sorted(set(old_fields) | set(new_fields)):
            if path not in new_fields:
                changes.append(OntologyChange.remove_field(resource_id, path))
            elif path not in old_fields or old_fields[path] != new_fields[path]:
                changes.append(
                    OntologyChange.set_field(
                        resource_id, path, new_fields[path]
                    )
                )
    return tuple(changes)
