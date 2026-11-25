"""Identity evidence contracts independent of the native execution engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class IdentityResolutionError(RuntimeError):
    """Raised when the native resolver cannot produce a durable decision."""


@dataclass(frozen=True, slots=True)
class SpatialEvidence:
    """WGS84 point and one-sigma location uncertainty."""

    latitude: float
    longitude: float
    accuracy_meters: float = 0.0


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """One PostgreSQL candidate and its optional LI-ESKG identity mapping."""

    entity_id: str
    similarity: float
    licorne_identity_id: int | None = None
    spatial: SpatialEvidence | None = None


@dataclass(frozen=True, slots=True)
class ResolutionRequest:
    """Immutable evidence submitted by a Vision actor for one extracted entity."""

    tenant_id: str
    observation_key: str
    source: str
    modality: str
    event_time_micros: int
    pipeline_probability: float
    candidates: tuple[CandidateEvidence, ...]
    spatial: SpatialEvidence | None = None
    vector_similarity_midpoint: float | None = None


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    """Normalized LI-ESKG result with the canonical PostgreSQL entity ID."""

    entity_id: str | None
    action: str
    licorne_identity_id: int | None
    observation_id: int
    decision_id: int
    inference_id: int
    probabilities: tuple[tuple[str, float], ...]
    selected_probability: float | None
    created_identity: bool
    final_version: int | None
    h3_cell: int | None
    iterations: int
    residual: float
    exact: bool


class IdentityResolver(Protocol):
    """Async resolution contract consumed by the compute pipeline."""

    async def resolve(self, request: ResolutionRequest) -> ResolutionDecision:
        """Evaluates one snapshot without durable writes, so confirmation can retry."""
        ...
