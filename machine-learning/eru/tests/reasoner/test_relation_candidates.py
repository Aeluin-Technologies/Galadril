"""Tests for generating validation rule structural candidates."""

from __future__ import annotations

from eru.common.types import CanonicalEntity
from eru.reasoner.relation_candidates import DefaultRelationCandidateGenerator
from eru.schema import GraphSchema, RelationConstraint
from pydantic import BaseModel


class EmptyModel(BaseModel):
    pass


def test_propose_filters_unsupported_cross_link_combinations() -> None:
    """Verifies connections missing targeted constraint declarations are filtered."""
    schema = GraphSchema(
        entity_model=EmptyModel,
        relation_model=EmptyModel,
        graph_model=EmptyModel,
        constraints=[
            RelationConstraint(
                relation="attacks",
                allowed_source={"UNIT"},
                allowed_target={"LOCATION"},
            )
        ],
    )
    generator = DefaultRelationCandidateGenerator(
        schema=schema, allow_self_loops=False
    )
    entities = [
        CanonicalEntity(
            canonical_name="1st Div",
            labels=["UNIT"],
            mentions=[],
            confidence=1.0,
            metadata={},
        ),
        CanonicalEntity(
            canonical_name="Base Alpha",
            labels=["LOCATION"],
            mentions=[],
            confidence=1.0,
            metadata={},
        ),
    ]

    candidates = generator.propose(entities)
    assert len(candidates) == 1
    assert candidates[0].source_id == "ent_0"
    assert candidates[0].target_id == "ent_1"
