"""Pydantic schemas for input validation and normalization in galadril-vision."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from galadril_vision.common.types import normalize_tenant_id


class CanonicalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = Field(default="unknown", min_length=1)
    storage_path: str | None = None
    event_type: str = Field(default="Observation", min_length=1)

    raw_payload: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("timestamp", "ingested_at", mode="before")
    @classmethod
    def _coerce_datetime(cls, v: Any) -> datetime:
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
