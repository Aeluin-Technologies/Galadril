"""Typed Python access to the internal Registry gRPC service."""

from .client import (
    OntologySliceArtifact,
    PipelineArtifact,
    RegistryClient,
    RegistryConfig,
    TenantArtifact,
    TenantValidation,
)

__all__ = [
    "OntologySliceArtifact",
    "PipelineArtifact",
    "RegistryClient",
    "RegistryConfig",
    "TenantArtifact",
    "TenantValidation",
]
