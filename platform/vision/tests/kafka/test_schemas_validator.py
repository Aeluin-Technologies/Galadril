"""Unit tests targeting Pydantic structural validation, payload normalizations, and batch filtering."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from typing import Any, cast

from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.connectors.kafka.consumer import IngestedMessage
from galadril_vision.connectors.kafka.schemas import (
    EventNormalizer,
    _modality_from_source,
)
from galadril_vision.connectors.kafka.validator import (
    validate_and_normalize_kafka_batch,
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
    now = datetime.now(timezone.utc)
    assert EventNormalizer._parse_timestamp(now) is now

    naive = datetime(2026, 3, 29, 12, 0, 0)
    assert EventNormalizer._parse_timestamp(naive).tzinfo == timezone.utc

    assert EventNormalizer._parse_timestamp(1774785600000) == datetime(
        2026, 3, 29, 12, 0, tzinfo=timezone.utc
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
    assert ctx_image["event_type"] == "observation"
    assert ctx_image["location_coords"] == [3.0, 3.0]

    ctx_text = EventNormalizer.normalize(base_payload, "text_source")
    assert ctx_text["event_type"] == "document_published"

    ctx_unknown = EventNormalizer.normalize(base_payload, "UNKNOWN")
    assert ctx_unknown["event_type"] == "observation"


@patch("galadril_vision.connectors.kafka.validator.CanonicalRecord")
def test_validate_and_normalize_kafka_batch_filtering(
    mock_canonical: MagicMock,
) -> None:
    """Tests sorting into accepted and rejected lists based on payload structures and validations."""
    mock_canonical.model_validate.return_value = MagicMock(spec=CanonicalRecord)

    valid_msg = IngestedMessage(
        topic="t",
        event_type="image_source",
        payload={
            "id": "id1",
            "tenant_id": "t1",
            "timestamp": 1000,
            "ingested_at": 1000,
            "source": "src",
        },
    )

    non_dict_msg = IngestedMessage(
        topic="t",
        event_type="image_source",
        payload=cast(dict[str, Any], "string_instead_of_dict"),
    )
    malformed_msg = IngestedMessage(
        topic="t", event_type="image_source", payload={"id": "id2"}
    )

    batch = [valid_msg, non_dict_msg, malformed_msg]
    result = validate_and_normalize_kafka_batch(batch)

    assert len(result.accepted) == 1
    assert len(result.rejected) == 2
    assert result.rejected[0].reason == "payload_not_dict"
    assert result.rejected[1].reason in (
        "pydantic_validation_error",
        "normalization_failed",
    )
