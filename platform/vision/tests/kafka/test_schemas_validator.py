"""Unit tests targeting FastStream payload normalization."""

from datetime import UTC, datetime

import pytest
from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.common.types import EventType
from galadril_vision.connectors.kafka.schemas import (
    EventNormalizer,
    _input_type_from_source,
)


def test_input_type_extraction_from_source_string() -> None:
    """Validates legacy registry source strings used as input classifications."""
    assert _input_type_from_source("image_source") == "image"
    assert _input_type_from_source("AUDIO_SOURCE") == "audio"
    assert _input_type_from_source("custom_pipeline_source") == "data"


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
    assert ctx_image["spatial"]["latitude"] == 3.0
    assert ctx_image["spatial"]["longitude"] == 3.0
    assert ctx_image["spatial"]["accuracy_meters"] > 0.0
    record = CanonicalRecord.model_validate(ctx_image)
    assert record.spatial is not None

    ctx_text = EventNormalizer.normalize(base_payload, "text_source")
    assert ctx_text["event_type"] == EventType.DOCUMENT_PUBLISHED.value

    ctx_unknown = EventNormalizer.normalize(base_payload, "UNKNOWN")
    assert ctx_unknown["event_type"] == EventType.OBSERVATION.value


def test_event_normalizer_preserves_li_eskg_observation_contract() -> None:
    """Carries lineage, both clocks, input type, and uncertainty into commands."""
    payload = {
        "id": "legacy-id",
        "authz": {"tenant": "tenant:alpha"},
        "observation": {
            "observation_id": "obs-stable-1",
            "source": {
                "source_id": "camera-east",
                "source_kind": "camera",
                "sensor_id": "cam-1",
                "sensor_type": "rgb",
                "device_id": None,
                "capture_id": "capture-9",
                "sequence_number": 4,
                "original_filename": "frame.jpg",
                "bucket": "bronze",
                "object_key": "alpha/frame.jpg",
            },
            "input_type": "IMAGE",
            "event_time": 1774785600000,
            "event_time_end": None,
            "ingestion_time": 1774785600100,
            "payload": {
                "uri": "s3://bronze/alpha/frame.jpg",
                "media_type": "image/jpeg",
                "size_bytes": 123,
                "content_hash": "etag-1",
                "hash_algorithm": "S3_ETAG",
                "encoding": None,
                "byte_offset": None,
                "byte_length": None,
            },
            "quality": {
                "confidence": 0.91,
                "calibration_id": "cal-v2",
                "localization_confidence": 0.87,
                "signal_to_noise_db": None,
                "sample_rate_hz": None,
                "frame_index": 4,
                "text_span_start": None,
                "text_span_end": None,
                "covariance": [1.0, 0.0, 0.0, 1.0],
                "attributes": {},
            },
            "spatial": {
                "reference_system": "WGS84",
                "latitude": 51.5,
                "longitude": -0.1,
                "altitude_meters": None,
                "accuracy_meters": 2.5,
                "geometry_wkt": None,
                "covariance": [1.0, 0.0, 0.0, 1.0],
            },
            "lineage": {
                "ingestion_id": "ing-1",
                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                "span_id": "00f067aa0ba902b7",
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                "tracestate": None,
                "source_event_id": "s3:ObjectCreated:Put",
                "correlation_id": "source-object-1",
                "schema_version": "3.0.0",
                "parent_observation_ids": [],
                "supersedes_observation_id": None,
                "idempotency_key": "obs-stable-1",
            },
            "fragment_id": "detection:0",
            "concurrent_group_id": "capture-9",
        },
    }

    normalized = EventNormalizer.normalize(payload, "image_source")
    record = CanonicalRecord.model_validate(normalized)

    assert record.record_id == "obs-stable-1"
    assert record.input_type == "image"
    assert record.modality == "data"
    assert record.source_metadata is not None
    assert record.source_metadata.sensor_id == "cam-1"
    assert record.payload_ref is not None
    assert record.payload_ref.content_hash == "etag-1"
    assert record.quality.covariance == (1.0, 0.0, 0.0, 1.0)
    assert record.lineage is not None
    assert record.concurrent_group_id == "capture-9"
    assert record.spatial is not None
    assert record.spatial.accuracy_meters == 2.5
    assert record.spatial.covariance == (1.0, 0.0, 0.0, 1.0)
