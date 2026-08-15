"""Shared data processing and serialization mapping helpers."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, cast

import cv2
import numpy as np
import structlog
from numpy.typing import NDArray

logger = structlog.get_logger(__name__)

_EMBEDDING_KEYS = frozenset(("embedding", "embeddings", "vector", "features"))
_METADATA_KEYS = frozenset(
    (
        "bbox",
        "confidence",
        "label",
        "model_name",
        "model_version",
        "mime_type",
        "modality",
        "source_field",
        "raw_modality",
    )
)
_MODEL_ARTIFACT_EXTENSIONS = frozenset(
    ("bin", "joblib", "model", "onnx", "pkl", "pt", "pth", "safetensors")
)
_IMAGE_EXTENSIONS = frozenset(
    ("bmp", "jpeg", "jpg", "png", "tif", "tiff", "webp")
)
_AUDIO_EXTENSIONS = frozenset(
    ("aac", "flac", "m4a", "mp3", "ogg", "opus", "wav")
)
_VIDEO_EXTENSIONS = frozenset(
    ("avi", "m4v", "mkv", "mov", "mp4", "mpeg", "webm")
)
_TEXT_EXTENSIONS = frozenset(
    ("csv", "json", "jsonl", "log", "md", "txt", "xml", "yaml", "yml")
)
_DOCUMENT_EXTENSIONS = frozenset(
    ("doc", "docx", "html", "pdf", "ppt", "pptx", "rtf", "xls", "xlsx")
)
_TEXT_PAYLOAD_KEYS = (
    "content",
    "text",
    "body",
    "transcript",
    "caption",
    "description",
)


def _pad_embedding_if_needed(
    vector: Any, expected_dim: int = 1024
) -> list[float] | None:
    """Pads 1D numerical embeddings with zero elements to match target array dims.

    Args:
        vector: Input list or numeric array.
        expected_dim: Required fixed output array sizing.

    Returns:
        A padded list representation or None if input evaluates blank.
    """
    if vector is None:
        return None

    v_arr = np.asarray(vector, dtype=np.float32)
    if v_arr.ndim != 1:
        v_arr = v_arr.ravel()

    current_dim = v_arr.shape[0]
    if current_dim == expected_dim:
        return cast(list[float], v_arr.tolist())

    if current_dim < expected_dim:
        pad_size = expected_dim - current_dim
        padded = [float(value) for value in v_arr]
        padded.extend([0.0] * pad_size)
        return padded

    raise ValueError(
        f"Embedding dimension {current_dim} exceeds maximum allowed limit of {expected_dim}."
    )


def _get_vector_dimensions(postgres_config: Any) -> int:
    """Extracts target vector dims attribute or falls back to system defaults."""
    raw_value = getattr(postgres_config, "vector_dimensions", 1024)
    try:
        dimensions = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("invalid_vector_dimensions_config", value=raw_value)
        dimensions = 1024
    return max(dimensions, 1)


def _get_vector_search_timeout_s(postgres_config: Any) -> float:
    """Transforms milliseconds timeout attribute into floating-point seconds."""
    raw_value = getattr(postgres_config, "vector_search_timeout_ms", 5000)
    try:
        timeout_ms = int(raw_value)
    except (TypeError, ValueError):
        timeout_ms = 5000
    return max(timeout_ms, 1) / 1000.0


def _get_param(params: Any, name: str, default: Any = None) -> Any:
    """Extracts a variable parameter from an underlying dict context or model field."""
    if params is None:
        return default
    if isinstance(params, dict):
        return params.get(name, default)
    return getattr(params, name, default)


def _normalize_model_key(value: Any, default: str = "default") -> str:
    """Normalizes model identifiers down to clean uniform alphanumeric tokens."""
    raw_value = value if isinstance(value, str) else default
    model_key = raw_value.strip().lower()
    if not model_key:
        model_key = default
    name = model_key.rsplit("/", 1)[-1]
    parts = name.rsplit(".", 1)
    if len(parts) == 2 and parts[1] in _MODEL_ARTIFACT_EXTENSIONS:
        return parts[0]
    return parts[-1]


def _normalize_data_modality(value: Any, default: str = "data") -> str:
    """Cleans and sanitizes data modality key names."""
    raw_value = value if isinstance(value, str) else default
    modality = raw_value.strip().lower()
    return modality or default


def _infer_modality(
    storage_path: Any,
    raw_payload: Any,
    metadata: Any,
    default: str = "data",
) -> str:
    """Deduces modality grouping context via metadata entries or target file extensions."""
    for container in (metadata, raw_payload):
        if not isinstance(container, dict):
            continue
        for key in (
            "modality",
            "input_type",
            "data_type",
            "media_type",
            "type",
        ):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return _normalize_data_modality(value)
        mime_type = container.get("mime_type") or container.get("content_type")
        if isinstance(mime_type, str) and "/" in mime_type:
            return _normalize_data_modality(mime_type.split("/", 1)[0])

    if isinstance(storage_path, str) and storage_path:
        suffix = PurePosixPath(storage_path).suffix.lower().lstrip(".")
        if suffix in _IMAGE_EXTENSIONS:
            return "image"
        if suffix in _AUDIO_EXTENSIONS:
            return "audio"
        if suffix in _VIDEO_EXTENSIONS:
            return "video"
        if suffix in _TEXT_EXTENSIONS:
            return "text"
        if suffix in _DOCUMENT_EXTENSIONS:
            return "document"

    return default


def _extract_text_payload(raw_payload: Any) -> str | None:
    """Extracts inline plain text properties out of raw unstructured payloads."""
    if not isinstance(raw_payload, dict):
        return None
    for key in _TEXT_PAYLOAD_KEYS:
        value = raw_payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _storage_location(
    storage_path: str, bucket: str, prefix: str
) -> tuple[str, str]:
    """Splits absolute cloud URIs or resolves path strings against an explicit base location."""
    if storage_path.startswith("s3://"):
        parts = storage_path[5:].split("/", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""
    return bucket, f"{prefix}/{storage_path}".strip("/")


def _decode_raw_content(
    content: bytes,
    modality: str,
    mime_type: str | None,
    record_id: Any,
) -> Any:
    """Decodes raw byte arrays based on the verified input modality type."""
    if modality == "image" or (mime_type or "").startswith("image/"):
        nparr = np.frombuffer(content, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image is None:
            logger.warning("image_decode_failed", record_id=record_id)
        return cast(NDArray[np.uint8] | None, image)
    if modality == "text" or (mime_type or "").startswith("text/"):
        return content.decode("utf-8", errors="replace")
    return content


def _build_raw_data_record(
    *,
    record_id: Any,
    storage_path: Any,
    raw_payload: Any,
    metadata: Any,
    content: Any,
    modality: str,
    mime_type: str | None,
) -> dict[str, Any]:
    """Constructs a consolidated payload envelope configuration."""
    return {
        "record_id": record_id,
        "storage_path": storage_path,
        "modality": modality,
        "mime_type": mime_type,
        "data": content,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "raw_payload": raw_payload if isinstance(raw_payload, dict) else {},
    }


def _is_numeric_embedding(value: Any) -> bool:
    """Verifies if an object is a populated one-dimensional numeric sequence."""
    if isinstance(value, np.ndarray):
        return value.ndim in (1, 2) and value.size > 0
    if not isinstance(value, (list, tuple)) or not value:
        return False
    return all(
        isinstance(item, (int, float, np.integer, np.floating))
        for item in value
    )


def _extract_embedding_items(
    prediction: Any, model_name: str
) -> list[dict[str, Any]]:
    """Traverses unstructured prediction dictionary collections to find matching embedding keys."""
    model_key = _normalize_model_key(model_name)

    if not isinstance(prediction, dict):
        return []

    faces = prediction.get("faces")
    if isinstance(faces, list):
        items = [
            item
            for item in faces
            if isinstance(item, dict) and item.get("embedding") is not None
        ]
        if items:
            for item in items:
                item.setdefault("model_name", model_key)
            return items

    extracted: list[dict[str, Any]] = []

    def _walk(node: Any, inherited: dict[str, Any]) -> None:
        if isinstance(node, dict):
            local_metadata = {
                key: node[key]
                for key in _METADATA_KEYS
                if key in node and key != "model_name"
            }
            metadata = {**inherited, **local_metadata}
            metadata["model_name"] = _normalize_model_key(
                node.get("model_name")
                or inherited.get("model_name")
                or model_key
            )

            for key in _EMBEDDING_KEYS:
                value = node.get(key)
                if _is_numeric_embedding(value):
                    item = dict(metadata)
                    item["embedding"] = value
                    extracted.append(item)
                elif key == "embeddings" and isinstance(value, list):
                    for embedding in value:
                        if _is_numeric_embedding(embedding):
                            item = dict(metadata)
                            item["embedding"] = embedding
                            extracted.append(item)

            next_metadata = {**inherited, **metadata}
            for key, value in node.items():
                if key not in _EMBEDDING_KEYS:
                    _walk(value, next_metadata)
        elif isinstance(node, list):
            if _is_numeric_embedding(node):
                return
            for value in node:
                _walk(value, inherited)

    _walk(prediction, {"model_name": model_key})
    return extracted


def _build_state_value(
    item: dict[str, Any],
    *,
    modality: str,
    model_name: str,
    event_id: str,
) -> dict[str, Any]:
    """Generates a sanitized state object mapping metrics of extracted entities."""
    state_value: dict[str, Any] = {
        "modality": modality,
        "model_name": model_name,
        "event_id": event_id,
    }
    for key in (
        "confidence",
        "bbox",
        "label",
        "model_version",
        "mime_type",
        "raw_modality",
        "source_field",
        "is_unknown",
        "resolution_action",
        "resolution_probability",
        "resolution_probabilities",
        "licorne_identity_id",
        "licorne_observation_id",
        "licorne_decision_id",
        "licorne_inference_id",
        "licorne_version",
        "licorne_created_identity",
        "licorne_iterations",
        "licorne_residual",
        "licorne_exact",
        "h3_cell",
    ):
        value = item.get(key)
        if value is not None:
            state_value[key] = value
    spatial = item.get("spatial")
    if isinstance(spatial, dict):
        latitude = spatial.get("latitude")
        longitude = spatial.get("longitude")
        accuracy = spatial.get("accuracy_meters")
        if isinstance(latitude, (int, float)) and isinstance(
            longitude, (int, float)
        ):
            state_value["lat"] = float(latitude)
            state_value["lon"] = float(longitude)
        if isinstance(accuracy, (int, float)):
            state_value["accuracy_meters"] = float(accuracy)
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and metadata:
        state_value["metadata"] = metadata
    return state_value
