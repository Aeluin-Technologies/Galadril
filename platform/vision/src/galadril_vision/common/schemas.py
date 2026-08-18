"""Pydantic schemas for input validation and normalization in galadril-vision."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from galadril_vision.common.types import normalize_tenant_id


class SpatialObservation(BaseModel):
    """Canonical point representation derived from source geometry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_system: str = Field(default="WGS84", min_length=1)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    altitude_meters: float | None = None
    accuracy_meters: float = Field(default=0.0, ge=0.0)
    geometry_wkt: str | None = None
    covariance: tuple[float, ...] = ()


class ObservationSource(BaseModel):
    """Physical and logical source metadata retained for sensor fusion."""

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


class PayloadReference(BaseModel):
    """Immutable source object or bounded fragment referenced by an observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_hash: str = Field(min_length=1)
    hash_algorithm: str = Field(min_length=1)
    encoding: str | None = None
    byte_offset: int | None = Field(default=None, ge=0)
    byte_length: int | None = Field(default=None, ge=0)


class ObservationQuality(BaseModel):
    """Modality-specific uncertainty retained without scalar collapse."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confidence: float | None = None
    calibration_id: str | None = None
    localization_confidence: float | None = None
    signal_to_noise_db: float | None = None
    sample_rate_hz: float | None = Field(default=None, ge=0.0)
    frame_index: int | None = Field(default=None, ge=0)
    text_span_start: int | None = Field(default=None, ge=0)
    text_span_end: int | None = Field(default=None, ge=0)
    covariance: tuple[float, ...] = ()
    attributes: dict[str, str] = Field(default_factory=dict)


class ObservationLineage(BaseModel):
    """Replay and tracing provenance carried downstream."""

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


class CanonicalRecord(BaseModel):
    """Validated evidence passed to the ESKG and LI-ESKG pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    timestamp_end: datetime | None = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = Field(default="unknown", min_length=1)
    storage_path: str | None = None
    event_type: str = Field(default="Observation", min_length=1)
    spatial: SpatialObservation | None = None
    input_type: str = Field(default="data", min_length=1)
    modality: str = Field(
        default="data",
        min_length=1,
        description="Model-defined embedding modality; intake leaves this as data.",
    )
    source_metadata: ObservationSource | None = None
    payload_ref: PayloadReference | None = None
    quality: ObservationQuality = Field(default_factory=ObservationQuality)
    lineage: ObservationLineage | None = None
    concurrent_group_id: str | None = None

    raw_payload: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("timestamp", "timestamp_end", "ingested_at", mode="before")
    @classmethod
    def _coerce_datetime(cls, v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=UTC)
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v) / 1000.0, tz=UTC)
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        return datetime.now(UTC)

    @field_validator("tenant_id", mode="before")
    @classmethod
    def _validate_tenant_id(cls, v: Any) -> str:
        return normalize_tenant_id(v)


class SchemaViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    record_id: str | None = None
    topic: str | None = None
    raw: dict[str, JsonValue] = Field(default_factory=dict)
