"""Shares the public GOL authoring sequence across real-service scenarios."""

import json
from dataclasses import dataclass

from assertions import require_mapping
from clients import GatewayClient


@dataclass(frozen=True, slots=True)
class GolPublication:
    """Pins source provenance independently from the compiled generation."""

    source_revision: str
    generation: str


async def publish_gol_source(
    gateway: GatewayClient,
    token: str,
    ontology_id: str,
    source: str,
    *,
    expected_revision: str | None = None,
    expected_generation: str | None = None,
) -> GolPublication:
    """Compiles exclusively in Registry; no local parser substitutes validation."""
    saved = await gateway.execute(
        token,
        "mutation($id:String!,$files:[GolSourceFileInput!]!,$expected:String){saveGolOntology(ontologyId:$id,displayName:$id,files:$files,expectedRevision:$expected){summary{sourceRevision}}}",
        {
            "id": ontology_id,
            "files": [{"path": "main.gol", "text": source}],
            "expected": expected_revision,
        },
    )
    summary = require_mapping(
        require_mapping(saved.get("saveGolOntology"), "saved GOL").get(
            "summary"
        ),
        "GOL summary",
    )
    revision = summary.get("sourceRevision")
    if not isinstance(revision, str) or not revision:
        raise AssertionError("Saved GOL source revision is missing")
    checked = await gateway.execute(
        token,
        "mutation($id:String!,$revision:String!){validateGolOntology(ontologyId:$id,sourceRevision:$revision){status diagnostics{code message}}}",
        {"id": ontology_id, "revision": revision},
    )
    validation = require_mapping(
        checked.get("validateGolOntology"), "GOL validation"
    )
    assert validation.get("status") == "valid", validation
    published = await gateway.execute(
        token,
        "mutation($id:String!,$revision:String!,$expected:String){publishGolOntology(ontologyId:$id,sourceRevision:$revision,expectedGeneration:$expected){sourceRevision generation}}",
        {
            "id": ontology_id,
            "revision": revision,
            "expected": expected_generation,
        },
    )
    receipt = require_mapping(
        published.get("publishGolOntology"), "GOL publication"
    )
    generation = receipt.get("generation")
    if not isinstance(generation, str) or not generation:
        raise AssertionError("Published GOL generation is missing")
    assert receipt.get("sourceRevision") == revision
    return GolPublication(revision, generation)


def gol_access(symbol: str) -> str:
    """Declares the fixture's approved external obligations as authored GOL."""
    permissions = "\n".join(
        f'    permission {local} {{ relation: "{external}"; cedar: {json.dumps(f'Gol::Action::"{external}"')}; }}'
        for local, external in (
            ("read_access", "read"),
            ("write_access", "write"),
            ("describe", "describe"),
            ("transfer", "transfer"),
            ("execute", "execute"),
        )
    )
    return (
        f'access {symbol} {{\n    resource: "gol_resource";\n{permissions}\n}}'
    )
