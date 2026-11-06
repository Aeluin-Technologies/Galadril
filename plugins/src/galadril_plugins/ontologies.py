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
    ontologies: list[dict[str, JsonValue]] | tuple[dict[str, JsonValue], ...],
) -> set[str]:
    identifiers: set[str] = set()
    for ontology in ontologies:
        for identifier in _module_resource_ids(ontology):
            if identifier in identifiers:
                raise ValueError("Duplicate package resource")
            identifiers.add(identifier)
    return identifiers


def _resource_objects(
    module: dict[str, JsonValue],
) -> list[dict[str, JsonValue]]:
    items = module.get("resources")
    if not isinstance(items, list) or not all(
        isinstance(item, dict) for item in items
    ):
        raise ValueError("Invalid ontology resources")
    return [item for item in items if isinstance(item, dict)]


def _resource_dependencies(item: dict[str, JsonValue]) -> set[str]:
    attributes = item["attributes"]
    if not isinstance(attributes, dict):
        raise ValueError("Invalid ontology attributes")
    dependencies: set[str] = set()
    for group in (
        item["references"],
        attributes.get("extends", []),
        attributes.get("interfaces", []),
    ):
        if not isinstance(group, list) or not all(
            isinstance(dep, str) for dep in group
        ):
            raise ValueError("Invalid ontology resource references")
        dependencies.update(dep for dep in group if isinstance(dep, str))
    for dep in (
        item.get("owner_id"),
        attributes.get("projection_of"),
        attributes.get("inverse_of"),
    ):
        if dep is not None:
            if not isinstance(dep, str):
                raise ValueError("Invalid ontology resource dependency")
            dependencies.add(dep)
    return dependencies


_ADMINISTRATIVE_VALUES = {
    "law.proceeding.procedure": {"avis_contentieux"},
    "law.legal_cause.kind": {"legalite_externe", "legalite_interne"},
    "law.plea_version.kind": {"detournement"},
    "law.deadline.kind": {"recours_administratif"},
}


def _branch_vocabulary(
    item: dict[str, JsonValue], branch: str
) -> dict[str, JsonValue]:
    resource = item["resource_id"]
    if resource == "law.court.judicial.kind":
        excluded = (
            {"cour_assises", "cour_criminelle_departementale"}
            if branch == "law.judiciaire"
            else {
                "tribunal_de_proximite",
                "conseil_de_prud_hommes",
                "tribunal_de_commerce",
                "tribunal_paritaire_des_baux_ruraux",
            }
        )
    else:
        excluded = _ADMINISTRATIVE_VALUES.get(str(resource))
    if excluded is None:
        return item
    attributes = item["attributes"]
    if not isinstance(attributes, dict) or not isinstance(
        values := attributes.get("allowed_values"), list
    ):
        raise ValueError("Missing litigation vocabulary")
    return {
        **item,
        "attributes": {
            **attributes,
            "allowed_values": [
                value
                for value in values
                if isinstance(value, str) and value not in excluded
            ],
        },
    }


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

    def litigation_views(self) -> tuple[OntologyPackage, ...]:
        """Keep the historical catalog and publish closed definitions for case use."""
        if self.id != "law.contentieux":
            return ()
        names = {
            "law.administratif": "Administrative litigation",
            "law.judiciaire": "Civil litigation",
            "law.penal": "Criminal proceedings",
        }
        return tuple(
            self._litigation_view(branch, name, set(names) - {branch})
            for branch, name in names.items()
        )

    def _litigation_view(
        self, branch: str, name: str, excluded_modules: set[str]
    ) -> OntologyPackage:
        administrative = branch == "law.administratif"
        if not administrative:
            excluded_modules.add("law.actes")
        excluded_resources = (
            {"law.court.judicial", "law.court.judicial.kind"}
            if administrative
            else {
                "law.proceeding.regime",
                "law.proceeding.suspends",
                "law.suspension_request",
                "law.suspension_effect",
            }
        )
        modules: list[dict[str, JsonValue]] = []
        for module in self.ontologies:
            if (
                isinstance(module["id"], str)
                and module["id"] in excluded_modules
            ):
                continue
            resources: list[JsonValue] = [
                item if administrative else _branch_vocabulary(item, branch)
                for item in _resource_objects(module)
                if str(item["resource_id"]) not in excluded_resources
            ]
            modules.append({**module, "resources": resources})
        while True:
            identifiers = _package_resource_ids(modules)
            removed = False
            for module in modules:
                kept: list[JsonValue] = []
                for item in _resource_objects(module):
                    if not identifiers.issuperset(_resource_dependencies(item)):
                        removed = True
                        continue
                    kept.append(item)
                module["resources"] = kept
            if not removed:
                break
        return OntologyPackage(
            id=branch,
            name=name,
            version=self.version,
            ontologies=tuple(
                module for module in modules if module["resources"]
            ),
            contracts=self.contracts if administrative else {},
        )
