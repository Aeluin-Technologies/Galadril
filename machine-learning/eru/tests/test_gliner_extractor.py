"""Tests for caller-owned GLiNER model loading."""

from __future__ import annotations

import pytest

from eru.common.exceptions import ExtractionError
from eru.extractor.gliner import GlinerExtractor


class FakeGliner:
    """Small GLiNER stand-in that avoids external model artifacts in tests."""

    __slots__ = ("labels",)

    def __init__(self) -> None:
        """Initializes fake model state."""
        self.labels: list[str] = []

    def to(self, _: str) -> "FakeGliner":
        """Mirrors torch module device movement without allocating tensors."""
        return self

    def encode_labels(self, labels: list[str]) -> list[str]:
        """Stores labels and returns reusable fake embeddings."""
        self.labels = labels
        return labels

    def batch_predict_with_embeds(
        self,
        texts: list[str],
        _: list[str],
        labels: list[str],
    ) -> list[list[dict[str, object]]]:
        """Returns one deterministic entity for the first input text."""
        return [
            [
                {
                    "text": texts[0][:4],
                    "start": 0,
                    "end": 4,
                    "score": 0.9,
                    "label": labels[0],
                }
            ]
        ]


def test_gliner_extractor_uses_supplied_model_without_path() -> None:
    extractor = GlinerExtractor(labels=["UNIT"], model=FakeGliner())

    result = extractor.extract("ABCD target")

    assert result[0].text == "ABCD"
    assert result[0].labels == ["UNIT"]


def test_gliner_extractor_requires_caller_owned_artifact() -> None:
    with pytest.raises(ExtractionError):
        GlinerExtractor(labels=["UNIT"])
