"""Typed Python access to the internal Registry gRPC service."""

from .client import (
    GolBindingArtifact,
    PipelineArtifact,
    RegistryClient,
    RegistryConfig,
    TenantArtifact,
    TenantValidation,
)

__all__ = [
    "GolBindingArtifact",
    "PipelineArtifact",
    "RegistryClient",
    "RegistryConfig",
    "TenantArtifact",
    "TenantValidation",
]
