"""Kafka message schemas and ESKG normalization."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum, unique
from typing import Any

from pydantic import BaseModel, Field

from galadril_vision.common.types import EventType, normalize_tenant_id


@unique
class InputType(StrEnum):
    """Supported homogeneous input types from Kafka."""

    IMAGE = "image"
    AUDIO = "audio"
    DOCUMENT = "document"
    TEXT = "text"
    TRANSACTION = "transaction"


class BoundingBox(BaseModel):
    """Geospatial bounding box."""

    top_left_lat: float
    top_left_lon: float
    bottom_right_lat: float
    bottom_right_lon: float


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


class ImageMessage(BaseEventMessage):
    mime_type: str | None = None
    geometry: BoundingBox | None = None


class AudioMessage(BaseEventMessage):
    duration_seconds: int | None = None
    language: str | None = None


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

        # Map registry IDs to internal core package system EventType values
        # where applicable. Defaults to the exact source.id string if no
        # hardcoded enum translation matches.
        mapped_type: str = EventType.OBSERVATION.value

        if resolved_event_type == "image_source":
            mapped_type = EventType.OBSERVATION.value
        elif resolved_event_type == "audio_source":
            mapped_type = EventType.COMMUNICATION.value
        elif resolved_event_type == "transaction_source":
            mapped_type = EventType.TRANSACTION.value
        elif resolved_event_type != "UNKNOWN":
            mapped_type = resolved_event_type

        context = {
            "record_id": payload.get("id"),
            "tenant_id": tenant_id,
            "timestamp": EventNormalizer._parse_timestamp(
                payload.get("timestamp")
            ),
            "ingested_at": EventNormalizer._parse_timestamp(
                payload.get("ingested_at")
            ),
            "storage_path": payload.get("storage_path"),
            "source": payload.get("source", "unknown"),
            "raw_payload": payload,
            "location_coords": None,
            "event_type": mapped_type,
        }

        if "geometry" in payload and payload["geometry"]:
            context["location_coords"] = (
                EventNormalizer._extract_center_from_bbox(payload["geometry"])
            )

        if context["location_coords"] is None:
            del context["location_coords"]

        return context

    @staticmethod
    def _parse_timestamp(ts_millis: int | datetime | None) -> datetime:
        """Convert Avro timestamp-millis to timezone-aware datetime."""
        if not ts_millis:
            return datetime.now(timezone.utc)
        if isinstance(ts_millis, datetime):
            if ts_millis.tzinfo is None:
                return ts_millis.replace(tzinfo=timezone.utc)
            return ts_millis
        return datetime.fromtimestamp(ts_millis / 1000.0, tz=timezone.utc)

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
        except KeyError:
            return None
