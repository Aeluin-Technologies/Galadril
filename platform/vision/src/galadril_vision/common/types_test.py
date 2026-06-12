"""Unit tests for shared multimodal type helpers."""

from __future__ import annotations

from galadril_vision.common.types import (
    EmbeddingModality,
    EntityEmbedding,
    normalize_embedding_modality,
)


def test_normalize_embedding_modality_supports_multimodal_values() -> None:
    assert normalize_embedding_modality(EmbeddingModality.AUDIO) == "audio"
    assert (
        normalize_embedding_modality("models/video_embedder.onnx")
        == "video_embedder"
    )
    assert normalize_embedding_modality(" text ") == "text"


def test_entity_embedding_defaults_to_data_modality() -> None:
    embedding = EntityEmbedding(tenant_id="tenant-a", vector=[0.1, 0.2])

    assert embedding.modality == EmbeddingModality.DATA
