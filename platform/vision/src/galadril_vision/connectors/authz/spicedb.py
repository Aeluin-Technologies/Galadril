"""SpiceDB (AuthZed) writer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from galadril_vision.common.config import SpiceDBConfig

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AuthzTuple:
    resource: str
    relation: str
    subject: str


class SpiceDBWriter:
    """
    Minimal SpiceDB relationship writer."""

    def __init__(self, cfg: SpiceDBConfig) -> None:
        self._cfg = cfg
        self._client = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client

        from authzed.api.v1 import client as az_client  # type: ignore

        self._client = az_client.Client(
            self._cfg.endpoint,
            token=self._cfg.token,
        )
        return self._client

    async def write_relationships(self, tuples: list[AuthzTuple]) -> None:
        """
        Write a batch of relationship tuples.

        Raises on failure; caller decides retry strategy.
        """
        if not tuples:
            return

        c = self._ensure_client()

        import asyncio

        await asyncio.to_thread(self._write_sync, c, tuples)

    def _write_sync(self, c: Any, tuples: list[AuthzTuple]) -> None:
        from authzed.api.v1 import permission_service_pb2 as ps_pb2  # type: ignore
        from authzed.api.v1 import core_pb2  # type: ignore
        from authzed.api.v1 import relationship_pb2 as rel_pb2  # type: ignore

        updates: list[rel_pb2.RelationshipUpdate] = []
        updates_extend = updates.append

        for t in tuples:
            # This assumes encoding object ids as strings like:
            #   resource="raw:topic:id" subject="group:analysts#member"
            # For now, we treat the resource/subject as "type:id" and split once.
            r_type, r_id = t.resource.split(":", 1)
            s_type, s_id = t.subject.split(":", 1)

            rel = rel_pb2.Relationship(
                resource=core_pb2.ObjectReference(
                    object_type=r_type, object_id=r_id
                ),
                relation=t.relation,
                subject=rel_pb2.SubjectReference(
                    object=core_pb2.ObjectReference(
                        object_type=s_type, object_id=s_id
                    )
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
