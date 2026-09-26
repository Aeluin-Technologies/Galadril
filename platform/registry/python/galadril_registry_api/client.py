"""Small typed asynchronous client for Registry runtime reads."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlsplit

import grpc
from google.protobuf.message import Message
from pydantic import BaseModel, ConfigDict, field_validator

from . import registry_pb2


class RegistryConfig(BaseModel):
    """Internal Registry endpoint for tenant and artifact reads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = "http://registry:50052"

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """Rejects credentials and storage-like paths at the client boundary."""
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Registry endpoint must be an HTTP authority")
        return value.rstrip("/")

    @property
    def target(self) -> str:
        """Returns the host and port expected by grpcio."""
        parsed = urlsplit(self.endpoint)
        return parsed.netloc


@dataclass(frozen=True, slots=True)
class PipelineArtifact:
    """Published pipeline pinned to an immutable Registry revision."""

    pipeline_id: str
    revision_id: str
    definition_json: bytes


@dataclass(frozen=True, slots=True)
class OntologySliceArtifact:
    """Dependency-closed ontology returned for one pinned pipeline block."""

    ontology_id: str
    publication_id: str
    revision_id: str
    ontology_json: bytes
    publication_metadata_json: bytes
    binding_metadata_json: bytes
    published_at_ms: int
    base_version: str
    base_hash: str
    effective_hash: str


@dataclass(frozen=True, slots=True)
class TenantArtifact:
    """Initialized tenant with its current opaque Registry revision."""

    tenant_id: str
    head_revision_id: str


@dataclass(frozen=True, slots=True)
class TenantValidation:
    """Physical S3 existence result for one caller-supplied tenant."""

    tenant_id: str
    exists: bool


class _PipelineMessage(Protocol):
    pipeline_id: str
    head_revision_id: str
    definition_json: bytes


class _PipelineListMessage(Protocol):
    pipelines: Sequence[_PipelineMessage]


class _OntologySliceMessage(Protocol):
    ontology_id: str
    publication_id: str
    revision_id: str
    ontology_json: bytes
    publication_metadata_json: bytes
    binding_metadata_json: bytes
    published_at_ms: int
    base_version: str
    base_hash: str
    effective_hash: str


class _TenantMessage(Protocol):
    tenant_id: str
    head_revision_id: str


class _TenantValidationMessage(Protocol):
    tenant_id: str
    exists: bool


class _TenantValidationListMessage(Protocol):
    tenants: Sequence[_TenantValidationMessage]


def _serialize(message: object) -> bytes:
    """Serializes generated protobufs without leaking dynamic types outward."""
    return cast(bytes, cast(Message, message).SerializeToString())


def _pipeline_list(data: bytes) -> object:
    """Decodes a generated list response behind a typed wrapper."""
    return registry_pb2.ListPipelinesResponse.FromString(data)


def _pipeline(data: bytes) -> object:
    """Decodes a generated pipeline response behind a typed wrapper."""
    return registry_pb2.Pipeline.FromString(data)


def _ontology_slice(data: bytes) -> object:
    """Decodes a generated ontology response behind a typed wrapper."""
    return registry_pb2.OntologySlice.FromString(data)


def _tenant(data: bytes) -> object:
    """Decodes a generated tenant response behind a typed wrapper."""
    return registry_pb2.Tenant.FromString(data)


def _tenant_validations(data: bytes) -> object:
    """Decodes bounded tenant validation results."""
    return registry_pb2.ValidateTenantsResponse.FromString(data)


def _delete_tenant(data: bytes) -> object:
    """Decodes the empty GDPR purge acknowledgement."""
    return registry_pb2.DeleteTenantResponse.FromString(data)


def _validated_tenant(value: str) -> str:
    """Rejects tenant identifiers that could escape server-side resolution."""
    if (
        not 1 <= len(value) <= 64
        or not value.isascii()
        or not all(
            character.isalnum() or character in "_-" for character in value
        )
    ):
        raise ValueError("Registry tenant contains unsupported characters")
    return value


class RegistryClient:
    """Owns one grpcio channel and exposes only typed Registry operations."""

    __slots__ = ("_channel",)

    def __init__(self, config: RegistryConfig) -> None:
        self._channel = grpc.aio.insecure_channel(config.target)

    async def close(self) -> None:
        """Closes the underlying HTTP/2 channel."""
        await self._channel.close()

    async def validate_tenants(
        self, tenant_ids: Sequence[str]
    ) -> tuple[TenantValidation, ...]:
        """Checks only the explicitly supplied tenant identities."""
        if not 1 <= len(tenant_ids) <= 100:
            raise ValueError("Tenant validation accepts 1 to 100 entries")
        values = tuple(_validated_tenant(value) for value in tenant_ids)
        response = cast(
            _TenantValidationListMessage,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/ValidateTenants",
                request_serializer=_serialize,
                response_deserializer=_tenant_validations,
            )(registry_pb2.ValidateTenantsRequest(tenant_ids=values)),
        )
        return tuple(
            TenantValidation(tenant_id=item.tenant_id, exists=item.exists)
            for item in response.tenants
        )

    async def get_tenant(self, tenant_id: str) -> TenantArtifact:
        """Reads one initialized tenant without creating storage."""
        item = cast(
            _TenantMessage,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/GetTenant",
                request_serializer=_serialize,
                response_deserializer=_tenant,
            )(
                registry_pb2.GetTenantRequest(
                    tenant_id=_validated_tenant(tenant_id)
                )
            ),
        )
        return TenantArtifact(item.tenant_id, item.head_revision_id)

    async def put_tenant(self, tenant_id: str) -> TenantArtifact:
        """Idempotently initializes one tenant's Registry storage."""
        item = cast(
            _TenantMessage,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/PutTenant",
                request_serializer=_serialize,
                response_deserializer=_tenant,
            )(
                registry_pb2.PutTenantRequest(
                    tenant_id=_validated_tenant(tenant_id)
                )
            ),
        )
        return TenantArtifact(item.tenant_id, item.head_revision_id)

    async def delete_tenant(
        self, tenant_id: str, *, confirmation_tenant_id: str
    ) -> None:
        """Permanently purges one tenant after an exact GDPR confirmation."""
        tenant = _validated_tenant(tenant_id)
        confirmation = _validated_tenant(confirmation_tenant_id)
        if tenant != confirmation:
            raise ValueError("Tenant purge confirmation mismatch")
        await self._channel.unary_unary(
            "/galadril.registry.v1.Registry/DeleteTenant",
            request_serializer=_serialize,
            response_deserializer=_delete_tenant,
        )(
            registry_pb2.DeleteTenantRequest(
                tenant_id=tenant, confirmation_tenant_id=confirmation
            )
        )

    async def list_published_pipelines(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[PipelineArtifact, ...]:
        """Lists validated publications with their pinned definitions."""
        response = cast(
            _PipelineListMessage,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/ListPublishedPipelines",
                request_serializer=_serialize,
                response_deserializer=_pipeline_list,
            )(
                registry_pb2.ListPublishedPipelinesRequest(
                    tenant_id=tenant_id, limit=limit
                )
            ),
        )
        return tuple(
            PipelineArtifact(
                pipeline_id=item.pipeline_id,
                revision_id=item.head_revision_id,
                definition_json=item.definition_json,
            )
            for item in response.pipelines
        )

    async def get_runtime_pipeline(
        self, tenant_id: str, pipeline_id: str, revision_id: str
    ) -> PipelineArtifact:
        """Loads exactly one published pipeline revision for execution."""
        item = cast(
            _PipelineMessage,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/GetRuntimePipeline",
                request_serializer=_serialize,
                response_deserializer=_pipeline,
            )(
                registry_pb2.GetRuntimePipelineRequest(
                    tenant_id=tenant_id,
                    pipeline_id=pipeline_id,
                    revision_id=revision_id,
                )
            ),
        )
        return PipelineArtifact(
            pipeline_id=item.pipeline_id,
            revision_id=item.head_revision_id,
            definition_json=item.definition_json,
        )

    async def get_ontology_slice(
        self,
        tenant_id: str,
        pipeline_id: str,
        pipeline_revision_id: str,
        block_id: str,
    ) -> OntologySliceArtifact:
        """Loads Registry-derived semantics at an immutable pipeline revision."""
        item = cast(
            _OntologySliceMessage,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/GetOntologySlice",
                request_serializer=_serialize,
                response_deserializer=_ontology_slice,
            )(
                registry_pb2.GetOntologySliceRequest(
                    tenant_id=tenant_id,
                    pipeline_id=pipeline_id,
                    pipeline_revision_id=pipeline_revision_id,
                    block_id=block_id,
                )
            ),
        )
        return OntologySliceArtifact(
            ontology_id=item.ontology_id,
            publication_id=item.publication_id,
            revision_id=item.revision_id,
            ontology_json=item.ontology_json,
            publication_metadata_json=item.publication_metadata_json,
            binding_metadata_json=item.binding_metadata_json,
            published_at_ms=item.published_at_ms,
            base_version=item.base_version,
            base_hash=item.base_hash,
            effective_hash=item.effective_hash,
        )
