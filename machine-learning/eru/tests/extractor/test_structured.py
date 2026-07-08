"""Tests for guided schema-based LLM token candidate extractions."""

from __future__ import annotations

import pytest
from eru.common.exceptions import ExtractionError
from eru.extractor.structured import (
    StructuredCandidateExtractor,
)


class MockStructuredModel:
    """Fakes target structured outputs using plain schemas."""

    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    def __call__(self, prompt: str, schema: type, max_new_tokens: int) -> dict:
        if self.should_fail:
            raise RuntimeError("LLM timed out.")
        return {
            "entities": [
                {
                    "text": "EU",
                    "labels": ["ORG"],
                    "start_char": 0,
                    "end_char": 2,
                    "confidence": 0.99,
                },
                {
                    "text": "Fake",
                    "labels": ["BAD_LABEL"],
                    "start_char": 3,
                    "end_char": 7,
                    "confidence": 0.8,
                },
                {
                    "text": "Miss",
                    "labels": ["ORG"],
                    "start_char": 100,
                    "end_char": 104,
                    "confidence": 0.8,
                },
                {
                    "text": "EU",
                    "labels": ["ORG"],
                    "start_char": 0,
                    "end_char": 2,
                    "confidence": 0.99,
                },
            ]
        }


def test_structured_extractor_parses_and_validates_spans() -> None:
    """Ensures invalid out-of-bounds tokens and illegal duplicates are filtered."""
    extractor = StructuredCandidateExtractor(
        model=MockStructuredModel(), labels=["ORG"]
    )
    results = extractor.extract("EU text context")

    assert len(results) == 1
    assert results[0].text == "EU"
    assert results[0].labels == ["ORG"]


def test_structured_extractor_exception_wrapping() -> None:
    """Verifies errors during generation convert to ExtractionError."""
    extractor = StructuredCandidateExtractor(
        model=MockStructuredModel(should_fail=True), labels=["ORG"]
    )
    with pytest.raises(ExtractionError):
        extractor.extract("text")
