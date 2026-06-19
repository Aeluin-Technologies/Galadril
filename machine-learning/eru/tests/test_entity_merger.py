"""Tests for robust entity merging without optional LLM enrichment."""

from __future__ import annotations

from eru.extractor.entity_merger import DefaultEntityMerger
from eru.common.types import EntityMention, ExtractedCandidate


def test_entity_merger_accepts_missing_normalization() -> None:
    merger = DefaultEntityMerger()
    candidate = ExtractedCandidate(
        text="CENTCOM",
        labels=["ORGANIZATION"],
        mentions=[
            EntityMention(
                text="CENTCOM",
                start_char=0,
                end_char=7,
                score=0.8,
            )
        ],
        confidence=0.8,
    )

    result = merger.merge([candidate], references=None, normalization=None)

    assert len(result) == 1
    assert result[0].canonical_name == "CENTCOM"
    assert result[0].labels == ["ORGANIZATION"]
