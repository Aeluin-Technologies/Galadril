"""Canonical definition of transactional pipeline message containers."""

from __future__ import annotations

import time
from typing import Generic, TypeVar
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class BatchHandle(BaseModel, Generic[T]):
    """Unified tracking container capturing processing windows, message offsets, and polymorphic step payloads."""

    model_config = ConfigDict(frozen=True)
    correlation_id: str
    kafka_offsets: dict[str, dict[int, int]] = Field(default_factory=dict)
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    payload: T


class PipelineResult(BaseModel):
    """Execution metrics emitted directly by the pure computational engine layer."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    processed_records: int
    duration: float
