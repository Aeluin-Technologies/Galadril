"""Integration tests evaluating the full orchestrated engine pipeline execution lifecycle."""

from __future__ import annotations

from typing import Any, cast

import pytest
from eru.common.types import (
    CanonicalEntity,
    EntityMention,
    ExtractedCandidate,
    RelationCandidate,
)
from eru.engine import EruEngine
from eru.schema import GraphSchema, RelationConstraint
from pydantic import BaseModel


class FinalEntity(BaseModel):
    id: str
    text: str
    type: str


class FinalRelation(BaseModel):
    source_id: str
    target_id: str
    relation_type: str


class FinalGraph(BaseModel):
    entities: list[FinalEntity]
    relations: list[FinalRelation]


class MockExtractor:
    def extract(self, text: str) -> list[ExtractedCandidate]:
        return [
            ExtractedCandidate(
                text="General",
                labels=["OFFICER"],
                mentions=[
                    EntityMention(
                        text="General", start_char=0, end_char=7, score=0.95
                    )
                ],
                confidence=0.95,
            ),
            ExtractedCandidate(
                text="Division",
                labels=["UNIT"],
                mentions=[
                    EntityMention(
                        text="Division", start_char=15, end_char=23, score=0.91
                    )
                ],
                confidence=0.91,
            ),
        ]


class MockRelationProposer:
    def propose(
        self, entities: list[CanonicalEntity]
    ) -> list[RelationCandidate]:
        return [RelationCandidate(source_id="ent_0", target_id="ent_1")]


class MockReasoner:
    def reason(
        self,
        text: str,
        entities: list[CanonicalEntity],
        candidates: list[RelationCandidate],
        schema: GraphSchema,
    ) -> FinalGraph:
        return FinalGraph(
            entities=[
                FinalEntity(id="ent_0", text="General", type="OFFICER"),
                FinalEntity(id="ent_1", text="Division", type="UNIT"),
            ],
            relations=[
                FinalRelation(
                    source_id="ent_0",
                    target_id="ent_1",
                    relation_type="commands",
                )
            ],
        )


class MockMerger:
    def merge(
        self, candidates: list, refs: None, norm: None
    ) -> list[CanonicalEntity]:
        return [
            CanonicalEntity(
                canonical_name="General",
                labels=["OFFICER"],
                mentions=[],
                confidence=0.95,
                metadata={},
            ),
            CanonicalEntity(
                canonical_name="Division",
                labels=["UNIT"],
                mentions=[],
                confidence=0.91,
                metadata={},
            ),
        ]


def test_eru_engine_full_pipeline_success() -> None:
    """Executes a text sample through all pipeline lifecycle processing stages."""
    schema = GraphSchema(
        entity_model=FinalEntity,
        relation_model=FinalRelation,
        graph_model=FinalGraph,
        constraints=[
            RelationConstraint(
                relation="commands",
                allowed_source={"OFFICER"},
                allowed_target={"UNIT"},
            )
        ],
    )

    engine = EruEngine[FinalGraph](
        schema=schema,
        extractor=cast(Any, MockExtractor()),
        reasoner=cast(Any, MockReasoner()),
        relation_candidates=cast(Any, MockRelationProposer()),
        entity_merger=MockMerger(),
    )

    graph = engine.process("General commands Division.")

    assert len(graph.entities) == 2
    assert len(graph.relations) == 1
    assert graph.relations[0].relation_type == "commands"


def test_eru_engine_rejects_empty_input() -> None:
    """Verifies that empty string vectors immediately reject execution processing."""
    schema = GraphSchema(
        entity_model=FinalEntity,
        relation_model=FinalRelation,
        graph_model=FinalGraph,
    )
    engine = EruEngine[FinalGraph](
        schema,
        extractor=cast(Any, MockExtractor()),
        reasoner=cast(Any, MockReasoner()),
        relation_candidates=cast(Any, MockRelationProposer()),
        entity_merger=MockMerger(),
    )

    with pytest.raises(ValueError, match="Empty input."):
        engine.process("   ")
