"""Unit tests for Kafka-to-ESKG multimodal normalization."""

from __future__ import annotations

from galadril_vision.connectors.kafka.schemas import EventNormalizer


def test_normalize_propagates_video_modality_metadata() -> None:
    normalized = EventNormalizer.normalize(
        {
            "id": "r1",
            "tenant_id": "tenant-a",
            "timestamp": 1710000000000,
            "ingested_at": 1710000000000,
            "storage_path": "video/r1.mp4",
            "source": "camera-1",
            "mime_type": "video/mp4",
        },
        "video_source",
    )

    assert normalized["event_type"] == "Observation"
    assert normalized["metadata"]["modality"] == "video"
    assert normalized["metadata"]["mime_type"] == "video/mp4"


def test_normalize_maps_text_source_to_document_event() -> None:
    normalized = EventNormalizer.normalize(
        {
            "id": "r2",
            "tenant_id": "tenant-a",
            "timestamp": 1710000000000,
            "ingested_at": 1710000000000,
            "source": "crawler",
            "content": "hello",
        },
        "text_source",
    )

    assert normalized["event_type"] == "DocumentPublished"
    assert normalized["metadata"]["modality"] == "text"
