"""Behavior tests for the canonical authorization contract."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep

import pytest
from authzed.api.v1 import (
    CheckPermissionRequest,
    CheckPermissionResponse,
    Consistency,
    InsecureClient,
    ObjectReference,
    Relationship,
    RelationshipUpdate,
    SubjectReference,
    WriteRelationshipsRequest,
    WriteSchemaRequest,
)
from grpc import RpcError

DockerContainer = pytest.importorskip(
    "testcontainers.core.container"
).DockerContainer

SPICEDB_IMAGE = "authzed/spicedb:v1.56.0"
TOKEN = "galadril-contract-test"
SCHEMA = (
    Path(__file__).parents[4] / "schemas" / "spicedb" / "schema.zed"
).read_text(encoding="utf-8")


@contextmanager
def _spicedb() -> Iterator[InsecureClient]:
    """Starts one isolated in-memory SpiceDB."""
    with (
        DockerContainer(SPICEDB_IMAGE)
        .with_command("serve-testing")
        .with_exposed_ports(50051) as container
    ):
        endpoint = (
            f"{container.get_container_host_ip()}:"
            f"{container.get_exposed_port(50051)}"
        )
        client = InsecureClient(endpoint, TOKEN)
        deadline = monotonic() + 20.0
        while True:
            try:
                client.WriteSchema(WriteSchemaRequest(schema=SCHEMA), timeout=2)
                break
            except RpcError:
                if monotonic() >= deadline:
                    raise
                sleep(0.05)
        yield client


@pytest.fixture(scope="module")
def spicedb() -> Iterator[InsecureClient]:
    """Shares the isolated datastore across pytest behavior cases."""
    with _spicedb() as client:
        yield client


def _relationship(
    resource_type: str,
    resource_id: str,
    relation: str,
    subject_type: str,
    subject_id: str,
    subject_relation: str = "",
) -> Relationship:
    return Relationship(
        resource=ObjectReference(
            object_type=resource_type,
            object_id=resource_id,
        ),
        relation=relation,
        subject=SubjectReference(
            object=ObjectReference(
                object_type=subject_type,
                object_id=subject_id,
            ),
            optional_relation=subject_relation,
        ),
    )


def _touch(client: InsecureClient, *relationships: Relationship) -> None:
    client.WriteRelationships(
        WriteRelationshipsRequest(
            updates=[
                RelationshipUpdate(
                    operation=RelationshipUpdate.Operation.OPERATION_TOUCH,
                    relationship=relationship,
                )
                for relationship in relationships
            ]
        ),
        timeout=20,
    )


def _delete(client: InsecureClient, relationship: Relationship) -> None:
    client.WriteRelationships(
        WriteRelationshipsRequest(
            updates=[
                RelationshipUpdate(
                    operation=RelationshipUpdate.Operation.OPERATION_DELETE,
                    relationship=relationship,
                )
            ]
        ),
        timeout=20,
    )


def _allowed(
    client: InsecureClient,
    resource_type: str,
    resource_id: str,
    permission: str,
    subject_type: str,
    subject_id: str,
) -> bool:
    response = client.CheckPermission(
        CheckPermissionRequest(
            consistency=Consistency(fully_consistent=True),
            resource=ObjectReference(
                object_type=resource_type,
                object_id=resource_id,
            ),
            permission=permission,
            subject=SubjectReference(
                object=ObjectReference(
                    object_type=subject_type,
                    object_id=subject_id,
                )
            ),
        ),
        timeout=20,
    )
    return int(response.permissionship) == int(
        CheckPermissionResponse.PERMISSIONSHIP_HAS_PERMISSION
    )


def test_membership_and_resource_parent_are_both_required(
    spicedb: InsecureClient,
) -> None:
    membership = _relationship("tenant", "tenant-a", "member", "user", "alice")
    _touch(
        spicedb,
        membership,
        _relationship(
            "document", "tenant-a/report", "parent", "tenant", "tenant-a"
        ),
        _relationship("document", "tenant-a/report", "owner", "user", "alice"),
    )

    assert _allowed(
        spicedb, "document", "tenant-a/report", "view", "user", "alice"
    )
    assert not _allowed(
        spicedb, "document", "tenant-a/report", "view", "user", "bob"
    )

    _delete(spicedb, membership)
    assert not _allowed(
        spicedb, "document", "tenant-a/report", "view", "user", "alice"
    )


def test_cross_tenant_membership_never_grants_resource_access(
    spicedb: InsecureClient,
) -> None:
    _touch(
        spicedb,
        _relationship("tenant", "tenant-b", "member", "user", "mallory"),
        _relationship(
            "document", "tenant-a/secret", "parent", "tenant", "tenant-a"
        ),
        _relationship(
            "document", "tenant-a/secret", "owner", "user", "mallory"
        ),
    )
    assert not _allowed(
        spicedb,
        "document",
        "tenant-a/secret",
        "view",
        "user",
        "mallory",
    )


def test_service_execution_is_pipeline_scoped(
    spicedb: InsecureClient,
) -> None:
    _touch(
        spicedb,
        _relationship(
            "pipeline", "tenant-a/vision", "parent", "tenant", "tenant-a"
        ),
        _relationship(
            "pipeline",
            "tenant-a/vision",
            "service_executor",
            "service",
            "vision",
        ),
    )
    assert _allowed(
        spicedb,
        "pipeline",
        "tenant-a/vision",
        "execute",
        "service",
        "vision",
    )
    assert not _allowed(
        spicedb,
        "pipeline",
        "tenant-a/other",
        "execute",
        "service",
        "vision",
    )


if __name__ == "__main__":
    with _spicedb() as test_client:
        test_membership_and_resource_parent_are_both_required(test_client)
        test_cross_tenant_membership_never_grants_resource_access(test_client)
        test_service_execution_is_pipeline_scoped(test_client)
