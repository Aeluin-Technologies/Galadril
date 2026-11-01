"""Data-only semantic packages shipped with trusted executable plugins."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


def _module_resource_ids(ontology: dict[str, JsonValue]) -> tuple[str, ...]:
    items = ontology.get("resources")
    if not isinstance(items, list) or len(items) > 256:
        raise ValueError("Invalid ontology resources")
    identifiers: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid ontology resource")
        identifier = item.get("resource_id")
        if not isinstance(identifier, str):
            raise ValueError("Invalid ontology resource")
        identifiers.append(identifier)
    return tuple(identifiers)


def _package_resource_ids(
    ontologies: tuple[dict[str, JsonValue], ...],
) -> set[str]:
    identifiers: set[str] = set()
    for ontology in ontologies:
        for identifier in _module_resource_ids(ontology):
            if identifier in identifiers:
                raise ValueError("Duplicate package resource")
            identifiers.add(identifier)
    return identifiers


class OntologyContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["optional", "required"] = "optional"
    resources: tuple[str, ...] = Field(default=(), max_length=256)


class OntologyPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*[.][a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    ontologies: tuple[dict[str, JsonValue], ...] = Field(
        min_length=1, max_length=32
    )
    contracts: dict[str, OntologyContract] = Field(
        default_factory=dict, max_length=128
    )

    @model_validator(mode="after")
    def bounded(self) -> OntologyPackage:
        import json

        if len(json.dumps(self.model_dump(mode="json")).encode()) > 512 * 1024:
            raise ValueError("Ontology package exceeds limit")
        resources = _package_resource_ids(self.ontologies)
        if len(resources) > 512:
            raise ValueError("Too many package resources")
        if any(
            identifier not in resources
            for contract in self.contracts.values()
            for identifier in contract.resources
        ):
            raise ValueError("Unknown ontology contract resource")
        return self
