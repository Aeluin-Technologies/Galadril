"""Tests for the default entity merger component."""

from __future__ import annotations

from typing import cast
from eru.common.types import (
    EntityMention,
    ExtractedCandidate,
    SemanticNormalization,
)
from eru.extractor.entity_merger import DefaultEntityMerger


def test_merge_without_normalization_rules() -> None:
    """Validates consolidation when no grounding rules are present."""
    merger = DefaultEntityMerger()
    candidates = [
        ExtractedCandidate(
            text="CENTCOM",
            labels=["ORG"],
            mentions=[
                EntityMention(
                    text="CENTCOM", start_char=10, end_char=17, score=0.9
                )
            ],
            confidence=0.9,
        ),
        ExtractedCandidate(
            text="CENTCOM",
            labels=["MILITARY"],
            mentions=[
                EntityMention(
                    text="CENTCOM", start_char=0, end_char=7, score=0.8
                )
            ],
            confidence=0.8,
        ),
    ]

    result = merger.merge(candidates, references=None, normalization=None)

    assert len(result) == 1
    entity = result[0]
    assert entity.canonical_name == "CENTCOM"
    assert entity.labels == ["MILITARY", "ORG"]
    assert entity.confidence == 0.9
    assert len(entity.mentions) == 2
    assert entity.mentions[0].start_char == 0
    assert entity.metadata["mention_count"] == 2


def test_merge_with_normalization_rules() -> None:
    """Validates clustering using predefined aliases and schema labels."""

    class FakeNormalizedEntity:
        canonical_name = "United States Central Command"
        aliases = ["CENTCOM", "USCENTCOM"]
        canonical_label = "ORGANIZATION"

    class FakeNormalization:
        entities = [FakeNormalizedEntity()]

    merger = DefaultEntityMerger()
    candidates = [
        ExtractedCandidate(
            text="CENTCOM",
            labels=["MILITARY"],
            mentions=[
                EntityMention(
                    text="CENTCOM", start_char=0, end_char=7, score=0.85
                )
            ],
            confidence=0.85,
        )
    ]

    result = merger.merge(
        candidates,
        references=None,
        normalization=cast(SemanticNormalization, FakeNormalization()),
    )

    assert len(result) == 1
    entity = result[0]
    assert entity.canonical_name == "United States Central Command"
    assert "ORGANIZATION" in entity.labels
