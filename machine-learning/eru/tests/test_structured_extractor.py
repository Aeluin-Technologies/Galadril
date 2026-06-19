"""Tests for shared-LLM candidate extraction."""

from __future__ import annotations
from typing import Any
from pydantic import BaseModel

from eru.extractor.structured import StructuredCandidateExtractor


class FakeStructuredModel:
    """Small callable backend that mimics the shared structured LLM."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        """Initializes call accounting."""
        self.calls = 0

    def __call__(
        self,
        _: str,
        schema: type[BaseModel],
        max_new_tokens: int,
    ) -> dict[str, Any]:
        """Returns deterministic extraction data through the requested schema."""
        self.calls += 1
        assert max_new_tokens == 1024
        payload = {
            "entities": [
                {
                    "text": "CENTCOM",
                    "labels": ["ORGANIZATION"],
                    "start_char": 0,
                    "end_char": 7,
                    "confidence": 0.8,
                },
                {
                    "text": "bad",
                    "labels": ["UNKNOWN"],
                    "start_char": 8,
                    "end_char": 11,
                    "confidence": 0.8,
                },
            ]
        }
        return schema.model_validate(payload).model_dump()


def test_structured_extractor_reuses_shared_model() -> None:
    model = FakeStructuredModel()
    extractor = StructuredCandidateExtractor(
        model=model,
        labels=["ORGANIZATION"],
    )

    result = extractor.extract("CENTCOM led operations")

    assert model.calls == 1
    assert len(result) == 1
    assert result[0].text == "CENTCOM"
    assert result[0].labels == ["ORGANIZATION"]
