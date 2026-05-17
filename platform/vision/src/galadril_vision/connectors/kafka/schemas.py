"""Kafka message schemas and ESKG normalization."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum, unique
from typing import Any

from pydantic import BaseModel, Field

from galadril_vision.common.types import EventType


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
    def normalize(payload: dict[str, Any]) -> dict[str, Any]:
        """
        Extracts the common base fields and maps specific fields to the ESKG Event semantics.
        """

        context = {
            "record_id": payload.get("id"),
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
            "event_type": EventType.OBSERVATION,  # Default
        }

        if "geometry" in payload:
            context["location_coords"] = (
                EventNormalizer._extract_center_from_bbox(payload["geometry"])
            )
            context["event_type"] = EventType.OBSERVATION

        elif "duration_seconds" in payload:
            context["event_type"] = EventType.COMMUNICATION

        elif "content" in payload:
            context["event_type"] = EventType.DOCUMENT_PUBLISHED

        elif "amount" in payload and "sender_account" in payload:
            context["event_type"] = EventType.TRANSACTION

        return context

    @staticmethod
    def _parse_timestamp(ts_millis: int | None) -> datetime:
        """Convert Avro timestamp-millis to timezone-aware datetime."""
        if not ts_millis:
            return datetime.now(timezone.utc)
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
