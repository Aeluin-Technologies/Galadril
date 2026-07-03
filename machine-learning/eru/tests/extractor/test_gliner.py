"""Tests for user-supplied GLiNER extraction pipelines."""

from __future__ import annotations

import pytest
from eru.common.exceptions import ExtractionError
from eru.extractor.gliner import GlinerExtractor


class MockGlinerModel:
    """Fakes internal GLiNER tensor execution for local pipeline isolation."""

    def encode_labels(self, labels: list[str]) -> list[str]:
        return labels

    def batch_predict_with_embeds(
        self, texts: list[str], embeds: list[str], labels: list[str]
    ) -> list[list[dict[str, object]]]:
        return [
            [
                {
                    "text": "NATO",
                    "start": 0,
                    "end": 4,
                    "score": 0.95,
                    "label": "ORG",
                },
                {
                    "text": "LowConf",
                    "start": 5,
                    "end": 12,
                    "score": 0.10,
                    "label": "ORG",
                },
            ]
        ]


def test_extract_filters_low_confidence_tokens() -> None:
    """Verifies tokens dropping below confidence floor are rejected."""
    extractor = GlinerExtractor(
        labels=["ORG"], model=MockGlinerModel(), threshold=0.5
    )
    candidates = extractor.extract("NATO LowConf")

    assert len(candidates) == 1
    assert candidates[0].text == "NATO"
    assert candidates[0].confidence == 0.95


def test_extract_returns_empty_list_for_whitespace_payloads() -> None:
    """Verifies internal model skip path triggers on blank input."""
    extractor = GlinerExtractor(labels=["ORG"], model=MockGlinerModel())
    assert extractor.extract("   \n  ") == []


def test_extractor_initialization_failure_wraps_exception() -> None:
    """Ensures configuration errors wrap cleanly inside ExtractionError."""
    with pytest.raises(ExtractionError):
        GlinerExtractor(labels=["ORG"], model_path=None, model=None)
