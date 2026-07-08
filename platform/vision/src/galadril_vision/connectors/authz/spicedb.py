"""SpiceDB relationship writer."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import structlog
from galadril_vision.common.config import SpiceDBConnectorConfig
from galadril_vision.common.exceptions import TenantIsolationError
from galadril_vision.common.types import (
    normalize_tenant_id,
    require_same_tenant,
)

logger = structlog.get_logger(__name__)

_OBJECT_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_RELATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AuthzTuple:
    tenant_id: str
    resource: str
    relation: str
    subject: str


class SpiceDBWriter:
    """Writes authorization relationship transformations to SpiceDB."""

    def __init__(
        self,
        cfg: SpiceDBConnectorConfig,
        subject_normalization_type: str | None = None,
    ) -> None:
        """Initializes the writer.

        Args:
            cfg: Configuration parameters for credentials and routing.
            subject_normalization_type: Optional fallback type string for plain subject names.
        """
        self._cfg = cfg
        self._subject_normalization_type = subject_normalization_type

        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        """Initializes and returns the client instance session."""
        if self._client is not None:
            return self._client

        async with self._lock:
            if self._client is not None:
                return self._client

            from authzed.api.v1 import AsyncClient  # type: ignore
            from grpcutil import (  # type: ignore
                bearer_token_credentials,
                insecure_bearer_token_credentials,
            )

            is_insecure = (
                "localhost" in self._cfg.endpoint
                or ":50051" in self._cfg.endpoint
            )

            if is_insecure:
                logger.info(
                    "connecting_to_spicedb_via_insecure_async_grpc",
                    endpoint=self._cfg.endpoint,
                )
                credentials = insecure_bearer_token_credentials(self._cfg.token)
            else:
                credentials = bearer_token_credentials(self._cfg.token)

            self._client = AsyncClient(
                self._cfg.endpoint,
                credentials,
            )
            return self._client

    def _split_reference(self, value: str, field_name: str) -> tuple[str, str]:
        """Validates and partitions reference targets into type and identifier parts."""
        if ":" not in value:
            if field_name == "subject" and self._subject_normalization_type:
                return self._subject_normalization_type, value
            raise TenantIsolationError(f"{field_name} is missing object type")

        object_type, object_id = value.split(":", 1)

        if not _OBJECT_TYPE_RE.fullmatch(object_type):
            raise TenantIsolationError(f"{field_name} object type is invalid")
        if not object_id:
            raise TenantIsolationError(f"{field_name} object id is empty")

        return object_type, object_id

    def _validate_tuple(
        self, tenant_id: str, authz_tuple: AuthzTuple
    ) -> AuthzTuple:
        """Validates the schema and multi-tenancy bounds of an authorization payload."""
        expected_tenant = require_same_tenant(tenant_id, authz_tuple.tenant_id)
        _, resource_id = self._split_reference(authz_tuple.resource, "resource")

        if not _RELATION_RE.fullmatch(authz_tuple.relation):
            raise TenantIsolationError("relation is invalid")

        if (
            resource_id != expected_tenant
            and not resource_id.startswith(f"{expected_tenant}/")
            and not resource_id.startswith(f"{expected_tenant}:")
        ):
            raise TenantIsolationError(
                "resource object id is not tenant scoped",
                tenant_id=expected_tenant,
            )

        subject_ref = authz_tuple.subject.split("#", 1)[0]
        self._split_reference(subject_ref, "subject")
        return authz_tuple

    async def write_relationships(
        self, tenant_id: str, tuples: list[AuthzTuple]
    ) -> None:
        """Validates and applies a collection of mutations to SpiceDB.

        Args:
            tenant_id: Expected multi-tenancy context boundary identifier.
            tuples: List of target definitions to record.
        """
        if not tuples:
            return

        tenant_id_val = normalize_tenant_id(tenant_id)
        validated = [self._validate_tuple(tenant_id_val, t) for t in tuples]

        client = await self._ensure_client()
        await self._write_async(client, tenant_id_val, validated)

    async def _write_async(
        self, client: Any, tenant_id: str, tuples: list[AuthzTuple]
    ) -> None:
        """Transforms tracking models into protobuf representations and submits them over gRPC."""
        from authzed.api.v1 import (  # type: ignore
            ObjectReference,
            Relationship,
            RelationshipUpdate,
            SubjectReference,
            WriteRelationshipsRequest,
        )

        updates: list[RelationshipUpdate] = []
        updates_append = updates.append

        for t in tuples:
            r_type, r_id = self._split_reference(t.resource, "resource")

            subject_ref, subject_relation = (
                t.subject.split("#", 1) if "#" in t.subject else (t.subject, "")
            )
            s_type, s_id = self._split_reference(subject_ref, "subject")

            rel = Relationship(
                resource=ObjectReference(object_type=r_type, object_id=r_id),
                relation=t.relation,
                subject=SubjectReference(
                    object=ObjectReference(object_type=s_type, object_id=s_id),
                    optional_relation=subject_relation,
                ),
            )

            updates_append(
                RelationshipUpdate(
                    operation=RelationshipUpdate.Operation.OPERATION_TOUCH,
                    relationship=rel,
                )
            )

        req = WriteRelationshipsRequest(updates=updates)
        await client.WriteRelationships(req)
