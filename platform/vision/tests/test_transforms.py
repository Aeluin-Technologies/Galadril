"""Unit tests for modality-agnostic Daft transform helpers."""

from __future__ import annotations

from galadril_vision.pipeline import transform_helpers


def test_infer_modality_prefers_explicit_metadata() -> None:
    metadata = {"modality": "video", "mime_type": "image/png"}
    raw_payload = {"type": "image"}

    assert (
        transform_helpers._infer_modality("capture.txt", raw_payload, metadata)
        == "video"
    )


def test_infer_modality_uses_mime_type_and_extension() -> None:
    assert (
        transform_helpers._infer_modality(None, {"mime_type": "audio/wav"}, {})
        == "audio"
    )
    assert (
        transform_helpers._infer_modality("s3://bucket/clip.mp4", {}, {})
        == "video"
    )
    assert transform_helpers._infer_modality("notes.md", {}, {}) == "text"
    assert transform_helpers._infer_modality("report.pdf", {}, {}) == "document"


def test_extract_text_payload_uses_supported_text_fields() -> None:
    assert (
        transform_helpers._extract_text_payload({"transcript": "hello"})
        == "hello"
    )
    assert transform_helpers._extract_text_payload({"bytes": b"hello"}) is None


def test_build_raw_data_record_preserves_source_context() -> None:
    record = transform_helpers._build_raw_data_record(
        record_id="r1",
        storage_path=None,
        raw_payload={"content": "hello"},
        metadata={"language": "en"},
        content="hello",
        modality="text",
        mime_type="text/plain",
    )

    assert record["record_id"] == "r1"
    assert record["data"] == "hello"
    assert record["modality"] == "text"
    assert record["metadata"] == {"language": "en"}
    assert record["raw_payload"] == {"content": "hello"}


def test_build_state_value_keeps_sparse_multimodal_metadata() -> None:
    state_value = transform_helpers._build_state_value(
        {
            "confidence": 0.91,
            "label": "contract",
            "model_version": "v1",
            "is_unknown": False,
        },
        modality="document",
        model_name="doc-embedder",
        event_id="evt_1",
    )

    assert state_value == {
        "modality": "document",
        "model_name": "doc-embedder",
        "event_id": "evt_1",
        "confidence": 0.91,
        "label": "contract",
        "model_version": "v1",
        "is_unknown": False,
    }
