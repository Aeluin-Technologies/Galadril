"""Unit tests targeting FastStream payload normalization."""

from datetime import UTC, datetime

import pytest
from galadril_vision.common.types import EventType
from galadril_vision.connectors.kafka.schemas import (
    EventNormalizer,
    _modality_from_source,
)


def test_modality_extraction_from_source_string() -> None:
    """Validates cleanup routines mapping registry source strings to input modalities."""
    assert _modality_from_source("image_source") == "image"
    assert _modality_from_source("AUDIO_SOURCE") == "audio"
    assert _modality_from_source("custom_pipeline_source") == "data"


def test_event_normalizer_tenant_id_extraction() -> None:
    """Verifies validation rules matching internal payload context definitions to prevent tenant mismatches."""
    payload_ok = {"tenant_id": "tn-1", "authz": {"tenant_id": "tn-1"}}
    assert EventNormalizer._extract_tenant_id(payload_ok) == "tn-1"

    payload_missing = {"source": "test"}
    with pytest.raises(ValueError, match="tenant_id is required"):
        EventNormalizer._extract_tenant_id(payload_missing)

    payload_mismatch = {"tenant_id": "tn-1", "authz": {"tenant": "tn-2"}}
    with pytest.raises(ValueError, match="tenant_id mismatch"):
        EventNormalizer._extract_tenant_id(payload_mismatch)


def test_event_normalizer_timestamp_parsing() -> None:
    """Ensures date strings, Unix timestamps, and fallback values resolve to timezone-aware datetimes."""
    now = datetime.now(UTC)
    assert EventNormalizer._parse_timestamp(now) is now

    naive = datetime(2026, 3, 29, 12, 0, 0)
    assert EventNormalizer._parse_timestamp(naive).tzinfo == UTC

    assert EventNormalizer._parse_timestamp(1774785600000) == datetime(
        2026, 3, 29, 12, 0, tzinfo=UTC
    )
    assert isinstance(EventNormalizer._parse_timestamp(None), datetime)


def test_event_normalizer_geospatial_center_approximation() -> None:
    """Validates geographical conversion bounds extracting center points from bounding box coordinates."""
    bbox = {
        "top_left_lat": 10.0,
        "top_left_lon": 20.0,
        "bottom_right_lat": 0.0,
        "bottom_right_lon": 10.0,
    }
    assert EventNormalizer._extract_center_from_bbox(bbox) == [5.0, 15.0]
    assert EventNormalizer._extract_center_from_bbox({"bad_key": 1.0}) is None
    assert EventNormalizer._extract_center_from_bbox(None) is None


def test_event_normalizer_normalize_mapping_variations() -> None:
    """Verifies semantic engine assignments and key cleanups matching registered input types."""
    base_payload = {
        "id": "uuid-1",
        "tenant_id": "t1",
        "timestamp": 1774785600000,
        "ingested_at": 1774785600000,
        "source": "sensor-a",
        "geometry": {
            "top_left_lat": 4.0,
            "top_left_lon": 4.0,
            "bottom_right_lat": 2.0,
            "bottom_right_lon": 2.0,
        },
    }

    ctx_image = EventNormalizer.normalize(base_payload, "image_source")
    assert ctx_image["event_type"] == EventType.OBSERVATION.value
    assert ctx_image["location_coords"] == [3.0, 3.0]

    ctx_text = EventNormalizer.normalize(base_payload, "text_source")
    assert ctx_text["event_type"] == EventType.DOCUMENT_PUBLISHED.value

    ctx_unknown = EventNormalizer.normalize(base_payload, "UNKNOWN")
    assert ctx_unknown["event_type"] == EventType.OBSERVATION.value
