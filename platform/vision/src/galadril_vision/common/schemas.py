"""Pydantic schemas for input validation and normalization in galadril-vision."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict, field_validator


class CanonicalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str = Field(..., min_length=1)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    ingested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    source: str = Field(default="unknown", min_length=1)
    storage_path: str | None = None
    event_type: str = Field(default="Observation", min_length=1)

    raw_payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", "ingested_at", mode="before")
    @classmethod
    def _coerce_datetime(cls, v: Any) -> datetime:
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v) / 1000.0, tz=timezone.utc)
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)


class SchemaViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    record_id: str | None = None
    topic: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ValidatedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: list[CanonicalRecord] = Field(default_factory=list)
    rejected: list[SchemaViolation] = Field(default_factory=list)
