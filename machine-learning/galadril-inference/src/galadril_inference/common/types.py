"""Core types for the inference library."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum, unique
from typing import Any
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _request_id() -> str:
    return uuid4().hex


@unique
class ModelStatus(StrEnum):
    """Lifecycle states of a model in the registry."""

    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ModelMeta:
    """Immutable identity card of a model.

    Attributes:
        name: Unique identifier used to dispatch predictions.
        version: Semantic version string (e.g. "1.2.0").
        description: Human-readable purpose of the model.
        tags: Arbitrary key-value metadata (team, domain, etc.).
        deprecated: If true, the model is kept for backward-compatibility but
            should not be used for new integrations.
    """

    name: str
    version: str
    description: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    deprecated: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ModelMeta.name cannot be empty.")
        if not self.version:
            raise ValueError("ModelMeta.version cannot be empty.")


@dataclass(frozen=True, slots=True)
class PredictionRequest:
    """A single inference request.

    Attributes:
        model_name: Which model should handle this request.
        features: The input feature dict — keys and types depend on the model.
        request_id: Correlation ID for tracing. Auto-generated if omitted.
        timestamp: When the request was created.
    """

    model_name: str
    features: dict[str, Any]
    request_id: str = field(default_factory=_request_id)
    timestamp: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.model_name:
            raise ValueError("PredictionRequest.model_name cannot be empty.")


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """The outcome of a single inference call."""

    model_name: str
    model_version: str
    prediction: Any
    confidence: float | None = None
    request_id: str = ""
    latency_ms: float = 0.0
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class ModelSummary:
    """Public model info for lightweight listing APIs."""

    name: str
    version: str
    description: str
    deprecated: bool = False
