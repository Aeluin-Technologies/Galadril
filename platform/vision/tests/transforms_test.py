"""Unit tests for pipeline transform helpers."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from galadril_vision.common.types import normalize_embedding_modality
from galadril_vision.pipeline.transforms import (
    _extract_embedding_items,
    _get_vector_dimensions,
    _normalize_model_key,
    _pad_embedding_if_needed,
)


def test_pad_embedding_extends_face_vector_to_postgres_dimension() -> None:
    vector = np.ones(512, dtype=np.float32)

    padded = _pad_embedding_if_needed(vector, expected_dim=1024)

    assert padded is not None
    assert len(padded) == 1024
    assert padded[:512] == [1.0] * 512
    assert padded[512:] == [0.0] * 512


def test_pad_embedding_rejects_vectors_larger_than_postgres_dimension() -> None:
    vector = np.ones(1025, dtype=np.float32)

    with pytest.raises(ValueError, match="exceeds maximum allowed"):
        _pad_embedding_if_needed(vector, expected_dim=1024)


def test_extract_embedding_items_supports_generic_prediction_payloads() -> None:
    prediction = {
        "detections": [
            {
                "label": "person",
                "bbox": [1, 2, 3, 4],
                "descriptor": {"embedding": [0.1, 0.2, 0.3]},
            }
        ]
    }

    items = _extract_embedding_items(prediction, "s3://models/ArcFace.onnx")

    assert items == [
        {
            "bbox": [1, 2, 3, 4],
            "label": "person",
            "model_name": "arcface",
            "embedding": [0.1, 0.2, 0.3],
        }
    ]


def test_extract_embedding_items_preserves_face_payloads() -> None:
    face = {"bbox": [0, 0, 10, 10], "embedding": [0.1, 0.2], "confidence": 0.9}

    items = _extract_embedding_items({"faces": [face]}, "facenet")

    assert items == [face]
    assert items[0]["model_name"] == "facenet"


def test_vector_dimension_defaults_and_reads_config() -> None:
    assert _get_vector_dimensions(SimpleNamespace(vector_dimensions=512)) == 512
    assert (
        _get_vector_dimensions(SimpleNamespace(vector_dimensions="bad")) == 1024
    )


def test_model_key_normalization_matches_common_modality_key() -> None:
    model_key = _normalize_model_key("FaceNet")

    assert model_key == "facenet"
    assert normalize_embedding_modality(model_key) == "facenet"
