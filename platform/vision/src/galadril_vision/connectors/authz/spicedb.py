"""SpiceDB (AuthZed) writer."""

from __future__ import annotations

import re
import threading
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
    """Minimal SpiceDB relationship writer."""

    def __init__(self, cfg: SpiceDBConnectorConfig) -> None:
        self._cfg = cfg
        self._client = None
        self._lock = threading.Lock()

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client

        with self._lock:
            if self._client is not None:
                return self._client

            from authzed.api.v1 import client as az_client  # type: ignore

            self._client = az_client.Client(
                self._cfg.endpoint,
                token=self._cfg.token,
            )
            return self._client

    def _split_reference(self, value: str, field_name: str) -> tuple[str, str]:
        if ":" not in value:
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
        return authz_tuple

    async def write_relationships(
        self, tenant_id: str, tuples: list[AuthzTuple]
    ) -> None:
        """
        Write a batch of relationship tuples.

        Raises on failure; caller decides retry strategy.
        """
        if not tuples:
            return

        tenant_id_val = normalize_tenant_id(tenant_id)
        validated = [self._validate_tuple(tenant_id_val, t) for t in tuples]
        c = self._ensure_client()

        import asyncio

        await asyncio.to_thread(self._write_sync, c, tenant_id_val, validated)

    def _write_sync(
        self, c: Any, tenant_id: str, tuples: list[AuthzTuple]
    ) -> None:
        from authzed.api.v1 import permission_service_pb2 as ps_pb2  # type: ignore
        from authzed.api.v1 import core_pb2  # type: ignore
        from authzed.api.v1 import relationship_pb2 as rel_pb2  # type: ignore

        updates: list[rel_pb2.RelationshipUpdate] = []
        updates_extend = updates.append

        for t in tuples:
            self._validate_tuple(tenant_id, t)
            r_type, r_id = self._split_reference(t.resource, "resource")
            subject_ref, subject_relation = (
                t.subject.split("#", 1) if "#" in t.subject else (t.subject, "")
            )
            s_type, s_id = self._split_reference(subject_ref, "subject")

            rel = rel_pb2.Relationship(
                resource=core_pb2.ObjectReference(
                    object_type=r_type, object_id=r_id
                ),
                relation=t.relation,
                subject=rel_pb2.SubjectReference(
                    object=core_pb2.ObjectReference(
                        object_type=s_type, object_id=s_id
                    ),
                    optional_relation=subject_relation,
                ),
            )

            updates_extend(
                rel_pb2.RelationshipUpdate(
                    operation=rel_pb2.RelationshipUpdate.OPERATION_TOUCH,
                    relationship=rel,
                )
            )

        req = ps_pb2.WriteRelationshipsRequest(updates=updates)
        c.WriteRelationships(req)
