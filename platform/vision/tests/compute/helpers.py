"""Unit tests evaluating shared data serialization and array mapping helpers."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from galadril_vision.compute.helpers import (
    _build_raw_data_record,
    _build_state_value,
    _decode_raw_content,
    _extract_embedding_items,
    _extract_text_payload,
    _get_param,
    _get_vector_dimensions,
    _get_vector_search_timeout_s,
    _infer_modality,
    _is_numeric_embedding,
    _normalize_data_modality,
    _normalize_model_key,
    _pad_embedding_if_needed,
    _storage_location,
)


class TestHelpersModule:
    """Validates structural array mappings, extractions, and conversions."""

    def test_pad_embedding_if_needed(self) -> None:
        """Tests dimension scaling, alignment adjustments, and input size constraints."""
        assert _pad_embedding_if_needed(None) is None

        exact = [1.0] * 1024
        assert _pad_embedding_if_needed(exact) == exact

        short = [1.0, 2.0]
        padded = _pad_embedding_if_needed(short, expected_dim=5)
        assert padded == [1.0, 2.0, 0.0, 0.0, 0.0]

        long = [1.0] * 10
        with pytest.raises(
            ValueError,
            match="Embedding dimension 10 exceeds maximum allowed limit",
        ):
            _pad_embedding_if_needed(long, expected_dim=5)

    def test_get_vector_dimensions(self) -> None:
        """Validates fallback mechanisms when configuration metadata fields are invalid."""
        mock_config = MagicMock()
        mock_config.vector_dimensions = 512
        assert _get_vector_dimensions(mock_config) == 512

        mock_config.vector_dimensions = "invalid_int"
        assert _get_vector_dimensions(mock_config) == 1024

    def test_get_vector_search_timeout_s(self) -> None:
        """Validates conversions from milliseconds config attributes to seconds."""
        mock_config = MagicMock()
        mock_config.vector_search_timeout_ms = 3000
        assert _get_vector_search_timeout_s(mock_config) == 3.0

        mock_config.vector_search_timeout_ms = "string_fail"
        assert _get_vector_search_timeout_s(mock_config) == 5.0

    def test_get_param(self) -> None:
        """Tests field extractions from either dictionary contexts or system classes."""
        assert _get_param(None, "key", "default") == "default"
        assert _get_param({"target": "value"}, "target") == "value"

        obj = MagicMock()
        obj.target = "attr_value"
        assert _get_param(obj, "target") == "attr_value"

    def test_normalize_model_key(self) -> None:
        """Ensures file paths, empty strings, and model definitions reduce to tokens."""
        assert _normalize_model_key(None) == "default"
        assert _normalize_model_key("   ") == "default"
        assert _normalize_model_key("path/to/model.onnx") == "model"
        assert _normalize_model_key("simple_model") == "simple_model"

    def test_normalize_data_modality(self) -> None:
        """Validates clean text sanitization for modality values."""
        assert _normalize_data_modality("  IMAGE  ") == "image"
        assert _normalize_data_modality(None, "fallback") == "fallback"

    def test_infer_modality(self) -> None:
        """Tests deduction mappings based on file suffix types or payload details."""
        meta = {"modality": "video"}
        assert _infer_modality("", {}, meta) == "video"

        meta_mime = {"mime_type": "image/jpeg"}
        assert _infer_modality("", {}, meta_mime) == "image"

        assert _infer_modality("file.png", {}, {}) == "image"
        assert _infer_modality("file.mp3", {}, {}) == "audio"
        assert _infer_modality("file.mp4", {}, {}) == "video"
        assert _infer_modality("file.txt", {}, {}) == "text"
        assert _infer_modality("file.pdf", {}, {}) == "document"
        assert (
            _infer_modality("unknown.xyz", {}, {}, default="fallback")
            == "fallback"
        )

    def test_extract_text_payload(self) -> None:
        """Verifies text field discoveries across unstructured payload blocks."""
        assert _extract_text_payload(None) is None
        assert (
            _extract_text_payload({"body": "extracted text"})
            == "extracted text"
        )
        assert _extract_text_payload({"irrelevant": "val"}) is None

    def test_storage_location(self) -> None:
        """Validates explicit parsing rules for S3 resource paths."""
        assert _storage_location("s3://my-bucket/my-key/path", "b", "p") == (
            "my-bucket",
            "my-key/path",
        )
        assert _storage_location("s3://bucket-only", "b", "p") == (
            "bucket-only",
            "",
        )
        assert _storage_location("relative/path", "b", "p") == (
            "b",
            "p/relative/path",
        )

    def test_decode_raw_content(self) -> None:
        """Evaluates content reconstruction across media modalities."""
        content = b"fake_bytes"
        with (
            patch(
                "galadril_vision.compute.helpers.cv2.imdecode",
                return_value="decoded_cv2",
            ),
            patch("galadril_vision.compute.helpers.np.frombuffer"),
        ):
            assert (
                _decode_raw_content(content, "image", None, "id")
                == "decoded_cv2"
            )

        with (
            patch(
                "galadril_vision.compute.helpers.cv2.imdecode",
                return_value=None,
            ),
            patch("galadril_vision.compute.helpers.np.frombuffer"),
        ):
            assert _decode_raw_content(content, "image", None, "id") is None

        text_content = b"hello"
        assert _decode_raw_content(text_content, "text", None, "id") == "hello"
        assert _decode_raw_content(content, "binary", None, "id") == content

    def test_build_raw_data_record(self) -> None:
        """Validates schema structure constraints for generated record envelopes."""
        rec = _build_raw_data_record(
            record_id="1",
            storage_path="s",
            raw_payload=None,
            metadata=None,
            content="c",
            modality="m",
            mime_type="mt",
        )
        assert rec["record_id"] == "1"
        assert rec["raw_payload"] == {}
        assert rec["metadata"] == {}

    def test_is_numeric_embedding(self) -> None:
        """Validates data type checks for numerical vector representations."""
        assert _is_numeric_embedding(np.array([1, 2])) is True
        assert _is_numeric_embedding([1, 2.5]) is True
        assert _is_numeric_embedding([]) is False
        assert _is_numeric_embedding(["not_a_number"]) is False

    def test_extract_embedding_items(self) -> None:
        """Validates extraction traversals through nested dictionnary objects."""
        assert _extract_embedding_items("not_a_dict", "m") == []

        faces_pred = {"faces": [{"embedding": [0.1], "confidence": 0.9}]}
        res = _extract_embedding_items(faces_pred, "model/name.onnx")
        assert len(res) == 1
        assert res[0]["model_name"] == "name"

        nested_pred = {
            "embedding": [0.2],
            "embeddings": [[0.3]],
            "child": {"vector": [0.4], "embeddings": "not_list"},
        }
        res_nested = _extract_embedding_items(nested_pred, "m")
        assert len(res_nested) >= 3

    def test_build_state_value(self) -> None:
        """Ensures dictionary metrics translate accurately to system states."""
        item = {"confidence": 0.8, "bbox": [1, 2], "metadata": {"meta": "data"}}
        sv = _build_state_value(
            item, modality="m", model_name="mn", event_id="evt"
        )
        assert sv["modality"] == "m"
        assert sv["confidence"] == 0.8
        assert sv["metadata"] == {"meta": "data"}
