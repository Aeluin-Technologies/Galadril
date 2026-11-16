"""Pure identity contracts; native runtime is imported only by its consumers."""

from .contracts import (
    CandidateEvidence,
    IdentityResolutionError,
    IdentityResolver,
    ResolutionDecision,
    ResolutionRequest,
    SpatialEvidence,
)

__all__ = [
    "CandidateEvidence",
    "IdentityResolutionError",
    "IdentityResolver",
    "ResolutionDecision",
    "ResolutionRequest",
    "SpatialEvidence",
]
