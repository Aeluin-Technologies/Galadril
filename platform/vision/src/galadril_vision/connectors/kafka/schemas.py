"""Kafka message schemas and ESKG normalization."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum, unique
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from galadril_vision.common.types import EventType, normalize_tenant_id


@unique
class InputType(StrEnum):
    """Supported incoming payload classifications."""

    IMAGE = "IMAGE"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"
    TEXT = "TEXT"
    TRANSACTION = "TRANSACTION"
    TABULAR = "TABULAR"
    STRUCTURED = "STRUCTURED"
    POINT_CLOUD = "POINT_CLOUD"
    DEPTH = "DEPTH"
    THERMAL = "THERMAL"
    RADAR = "RADAR"
    LIDAR = "LIDAR"
    SENSOR = "SENSOR"
    BINARY = "BINARY"


class BoundingBox(BaseModel):
    """Geospatial bounding box."""

    top_left_lat: float
    top_left_lon: float
    bottom_right_lat: float
    bottom_right_lon: float


class SourceDescriptor(BaseModel):
    """Physical and logical source metadata captured by intake."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    sensor_id: str | None = None
    sensor_type: str | None = None
    device_id: str | None = None
    capture_id: str | None = None
    sequence_number: int | None = None
    original_filename: str | None = None
    bucket: str = Field(min_length=1)
    object_key: str = Field(min_length=1)


class PayloadReferenceMessage(BaseModel):
    """Avro representation of an immutable payload reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_hash: str = Field(min_length=1)
    hash_algorithm: str = Field(min_length=1)
    encoding: str | None = None
    byte_offset: int | None = Field(default=None, ge=0)
    byte_length: int | None = Field(default=None, ge=0)


class QualityMetadataMessage(BaseModel):
    """Typed detector, signal, text, and covariance quality metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confidence: float | None = None
    calibration_id: str | None = None
    localization_confidence: float | None = None
    signal_to_noise_db: float | None = None
    sample_rate_hz: float | None = None
    frame_index: int | None = None
    text_span_start: int | None = None
    text_span_end: int | None = None
    covariance: tuple[float, ...] = ()
    attributes: dict[str, str] = Field(default_factory=dict)


class SpatialContextMessage(BaseModel):
    """Source spatial data with optional non-scalar uncertainty."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_system: str = "WGS84"
    latitude: float | None = None
    longitude: float | None = None
    altitude_meters: float | None = None
    accuracy_meters: float | None = None
    geometry_wkt: str | None = None
    covariance: tuple[float, ...] = ()


class LineageMetadataMessage(BaseModel):
    """Trace and replay context spanning intake and galadril-vision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ingestion_id: str = Field(min_length=1)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    traceparent: str | None = None
    tracestate: str | None = None
    source_event_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    parent_observation_ids: tuple[str, ...] = ()
    supersedes_observation_id: str | None = None
    idempotency_key: str = Field(min_length=1)


class ObservationContextMessage(BaseModel):
    """Shared Avro evidence contract consumed before identity inference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(min_length=1)
    source: SourceDescriptor
    input_type: InputType
    event_time: int | datetime
    event_time_end: int | datetime | None = None
    ingestion_time: int | datetime
    payload: PayloadReferenceMessage
    quality: QualityMetadataMessage = Field(
        default_factory=QualityMetadataMessage
    )
    spatial: SpatialContextMessage | None = None
    lineage: LineageMetadataMessage
    fragment_id: str | None = None
    concurrent_group_id: str | None = None


class BaseEventMessage(BaseModel):
    """Common fields guaranteed by all Galadril Avro schemas."""

    id: str = Field(..., description="Global UUID.")
    timestamp: int = Field(
        ..., description="Unix timestamp millis of occurrence."
    )
    ingested_at: int = Field(
        ..., description="Unix timestamp millis of ingestion."
    )
    storage_path: str | None = Field(default=None, description="S3/MinIO URI.")
    source: str = Field(..., description="Origin of the data.")
    observation: ObservationContextMessage | None = None


class ImageMessage(BaseEventMessage):
    mime_type: str | None = None
    geometry: BoundingBox | None = None


class AudioMessage(BaseEventMessage):
    duration_seconds: int | None = None
    language: str | None = None


class VideoMessage(BaseEventMessage):
    """Video clip metadata retaining its source payload reference."""

    mime_type: str | None = None
    duration_millis: int | None = None
    width_pixels: int | None = None
    height_pixels: int | None = None
    frame_rate_hz: float | None = None
    codec: str | None = None
    audio_present: bool | None = None


class DocumentMessage(BaseEventMessage):
    original_filename: str | None = None
    mime_type: str | None = None
    file_hash: str | None = None


class TextMessage(BaseEventMessage):
    content: str
    url: str | None = None
    author: str | None = None


class TransactionMessage(BaseEventMessage):
    sender_account: str | None = None
    receiver_account: str | None = None
    amount: float | None = None
    currency: str | None = None
    transaction_type: str | None = None


class SensorMessage(BaseEventMessage):
    """Generic scalar or multidimensional sensor observation."""

    measurement_type: str
    value: float | None = None
    unit: str | None = None
    dimensions: dict[str, float] = Field(default_factory=dict)
    sampling_rate_hz: float | None = None
    sample_count: int | None = None
    unit_system: str | None = None


_INPUT_TYPE_VALUES = frozenset(item.value.lower() for item in InputType)


def _input_type_from_source(resolved_event_type: str) -> str:
    """Derives a legacy input classification from source registry naming."""
    source_type = resolved_event_type.strip().lower()
    if source_type.endswith("_source"):
        source_type = source_type[: -len("_source")]
    if source_type in _INPUT_TYPE_VALUES:
        return source_type
    return "data"


class EventNormalizer:
    """Normalizes homogeneous Avro schemas into a unified ESKG context."""

    @staticmethod
    def _extract_tenant_id(payload: dict[str, Any]) -> str:
        """Extracts and validates the tenant ID from the payload structural contexts."""
        authz = payload.get("authz")
        candidates: list[str] = []

        top_level_tenant = payload.get("tenant_id")
        if isinstance(top_level_tenant, str):
            candidates.append(normalize_tenant_id(top_level_tenant))

        if isinstance(authz, dict):
            for key in ("tenant_id", "tenant"):
                authz_tenant = authz.get(key)
                if isinstance(authz_tenant, str):
                    candidates.append(normalize_tenant_id(authz_tenant))

        if not candidates:
            raise ValueError("tenant_id is required")

        first = candidates[0]
        for tenant_id in candidates[1:]:
            if tenant_id != first:
                raise ValueError("tenant_id mismatch in Kafka payload")
        return first

    @staticmethod
    def normalize(
        payload: dict[str, Any], resolved_event_type: str
    ) -> dict[str, Any]:
        """Maps specific fields to the ESKG Event semantics using determined registry contexts.

        Args:
            payload: Raw deserialized Avro dictionary data content.
            resolved_event_type: The source ID string extracted via the Schema Registry.

        Returns:
            A unified dictionary layout normalized for downstream engine consumption.
        """
        tenant_id = EventNormalizer._extract_tenant_id(payload)
        observation = EventNormalizer._observation_context(payload)
        input_type = (
            observation.input_type.value.lower()
            if observation is not None
            else _input_type_from_source(resolved_event_type)
        )
        mapped_type = EventNormalizer._event_type(
            input_type, resolved_event_type
        )

        timestamp = (
            observation.event_time
            if observation is not None
            else payload.get("timestamp")
        )
        ingested_at = (
            observation.ingestion_time
            if observation is not None
            else payload.get("ingested_at")
        )
        source = (
            observation.source.source_id
            if observation is not None
            else payload.get("source", "unknown")
        )
        storage_path = (
            observation.payload.uri
            if observation is not None
            else payload.get("storage_path")
        )

        context = {
            "record_id": (
                observation.observation_id
                if observation is not None
                else payload.get("id")
            ),
            "tenant_id": tenant_id,
            "timestamp": EventNormalizer._parse_timestamp(timestamp),
            "timestamp_end": (
                EventNormalizer._parse_timestamp(observation.event_time_end)
                if observation is not None
                and observation.event_time_end is not None
                else None
            ),
            "ingested_at": EventNormalizer._parse_timestamp(ingested_at),
            "storage_path": storage_path,
            "source": source,
            "raw_payload": payload,
            "metadata": {
                "input_type": input_type,
                "mime_type": payload.get("mime_type"),
                "language": payload.get("language"),
                "source_kind": (
                    observation.source.source_kind
                    if observation is not None
                    else None
                ),
            },
            "input_type": input_type,
            "modality": "data",
            "source_metadata": (
                observation.source.model_dump(mode="json")
                if observation is not None
                else None
            ),
            "payload_ref": (
                observation.payload.model_dump(mode="json")
                if observation is not None
                else None
            ),
            "quality": (
                observation.quality.model_dump(mode="json")
                if observation is not None
                else {}
            ),
            "lineage": (
                observation.lineage.model_dump(mode="json")
                if observation is not None
                else None
            ),
            "concurrent_group_id": (
                observation.concurrent_group_id
                if observation is not None
                else None
            ),
            "spatial": None,
            "event_type": mapped_type,
        }

        if observation is not None and observation.spatial is not None:
            context["spatial"] = EventNormalizer._extract_observation_spatial(
                observation.spatial
            )
        elif "geometry" in payload and payload["geometry"]:
            context["spatial"] = EventNormalizer._extract_spatial_from_bbox(
                payload["geometry"]
            )

        if context["spatial"] is None:
            del context["spatial"]

        return context

    @staticmethod
    def _observation_context(
        payload: dict[str, Any],
    ) -> ObservationContextMessage | None:
        """Validates the shared evidence contract when intake supplied it."""
        raw_context = payload.get("observation")
        if raw_context is None:
            return None
        return ObservationContextMessage.model_validate(raw_context)

    @staticmethod
    def _event_type(input_type: str, resolved_event_type: str) -> str:
        """Maps the input classification onto the host ESKG event profile."""
        if input_type == "audio":
            return EventType.COMMUNICATION.value
        if input_type in {"text", "document"}:
            return EventType.DOCUMENT_PUBLISHED.value
        if input_type == "transaction":
            return EventType.TRANSACTION.value
        if resolved_event_type not in {"UNKNOWN", ""} and not (
            resolved_event_type.endswith("_source")
        ):
            return resolved_event_type
        return EventType.OBSERVATION.value

    @staticmethod
    def _parse_timestamp(
        ts_millis: int | float | str | datetime | None,
    ) -> datetime:
        """Convert Avro timestamp-millis to timezone-aware datetime."""
        if ts_millis is None:
            return datetime.now(UTC)
        if isinstance(ts_millis, datetime):
            if ts_millis.tzinfo is None:
                return ts_millis.replace(tzinfo=UTC)
            return ts_millis
        if isinstance(ts_millis, str):
            parsed = datetime.fromisoformat(ts_millis.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return datetime.fromtimestamp(ts_millis / 1000.0, tz=UTC)

    @staticmethod
    def _extract_observation_spatial(
        spatial: SpatialContextMessage,
    ) -> dict[str, Any] | None:
        """Projects a typed spatial context into the canonical point view."""
        if spatial.latitude is None or spatial.longitude is None:
            return None
        if not (-90.0 <= spatial.latitude <= 90.0):
            return None
        if not (-180.0 <= spatial.longitude <= 180.0):
            return None
        return {
            "reference_system": spatial.reference_system,
            "latitude": spatial.latitude,
            "longitude": spatial.longitude,
            "altitude_meters": spatial.altitude_meters,
            "accuracy_meters": spatial.accuracy_meters or 0.0,
            "geometry_wkt": spatial.geometry_wkt,
            "covariance": spatial.covariance,
        }

    @staticmethod
    def _extract_center_from_bbox(
        geometry: dict[str, float] | None,
    ) -> list[float] | None:
        """Approximates center [lat, lon] from bounding box for PostGIS point."""
        if not geometry:
            return None
        try:
            lat = (
                geometry["top_left_lat"] + geometry["bottom_right_lat"]
            ) / 2.0
            lon = (
                geometry["top_left_lon"] + geometry["bottom_right_lon"]
            ) / 2.0
            return [lat, lon]
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _extract_spatial_from_bbox(
        geometry: dict[str, float] | None,
    ) -> dict[str, float] | None:
        """Converts an Avro bounding box to a center and uncertainty radius."""
        center = EventNormalizer._extract_center_from_bbox(geometry)
        if center is None or geometry is None:
            return None
        latitude, longitude = center
        if not (-90.0 <= latitude <= 90.0) or not (
            -180.0 <= longitude <= 180.0
        ):
            return None
        try:
            accuracy_meters = _haversine_meters(
                latitude,
                longitude,
                float(geometry["top_left_lat"]),
                float(geometry["top_left_lon"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_meters": accuracy_meters,
        }


def _haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Returns great-circle distance without allocating geometry objects."""
    radius_meters = 6_371_008.8
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    haversine = math.sin(delta_lat / 2.0) ** 2 + (
        math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * radius_meters * math.asin(min(1.0, math.sqrt(haversine)))
