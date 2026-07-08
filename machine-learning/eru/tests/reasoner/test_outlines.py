"""Tests for guided structural reasoner graph evaluation modules."""

from __future__ import annotations

from typing import cast

from eru.common.types import CanonicalEntity, RelationCandidate, RelationDef
from eru.reasoner.outlines import OutlinesReasoner
from eru.schema import GraphSchema
from pydantic import BaseModel


class MockEntity(BaseModel):
    id: str
    text: str
    type: str


class MockRelation(BaseModel):
    source_id: str
    target_id: str
    relation_type: str


class MockGraph(BaseModel):
    entities: list[MockEntity]
    relations: list[MockRelation]


def test_reason_constructs_schema_compliant_graph() -> None:
    """Ensures generated extraction arrays match target validation fields."""

    def mock_model(prompt, schema, max_new_tokens):
        return {
            "relations": [
                {
                    "source_id": "ent_0",
                    "target_id": "ent_1",
                    "relation_type": "located_at",
                }
            ]
        }

    schema = GraphSchema(
        entity_model=MockEntity,
        relation_model=MockRelation,
        graph_model=MockGraph,
        relation_defs=[
            RelationDef(
                name="located_at", description="Entity placement location"
            )
        ],
    )

    reasoner = OutlinesReasoner(model=mock_model)
    entities = [
        CanonicalEntity(
            canonical_name="HQ",
            labels=["ORG"],
            mentions=[],
            confidence=1.0,
            metadata={},
        ),
        CanonicalEntity(
            canonical_name="Paris",
            labels=["LOC"],
            mentions=[],
            confidence=1.0,
            metadata={},
        ),
    ]
    candidates = [RelationCandidate(source_id="ent_0", target_id="ent_1")]

    graph = cast(
        MockGraph, reasoner.reason("text", entities, candidates, schema)
    )
    assert len(graph.entities) == 2
    assert len(graph.relations) == 1
    assert graph.relations[0].relation_type == "located_at"
