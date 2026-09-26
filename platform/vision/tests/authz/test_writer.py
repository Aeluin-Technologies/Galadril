"""Unit tests targeting the SpiceDB relationship writer and gRPC payload generators."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.common.config import SpiceDBConnectorConfig
from galadril_vision.common.exceptions import TenantIsolationError
from galadril_vision.connectors.authz.spicedb import (
    AuthzTuple,
    SpiceDBWriter,
    canonical_spicedb_object_id,
)


@pytest.fixture(autouse=True)
def setup_mock_modules() -> None:
    """Pre-populates runtime system modules to intercept third-party library imports."""
    mock_authzed = MagicMock()
    mock_grpcutil = MagicMock()
    sys.modules["authzed.api.v1"] = mock_authzed
    sys.modules["grpcutil"] = mock_grpcutil


@pytest.fixture
def mock_spicedb_config() -> SpiceDBConnectorConfig:
    """Provides a base configuration template for SpiceDB connection profiles."""
    config = MagicMock(spec=SpiceDBConnectorConfig)
    config.endpoint = "spicedb:50051"
    config.token = "secret_grpc_token"
    config.insecure = True
    config.max_local_retries = 3
    config.base_retry_ms = 100
    config.max_retry_ms = 1000
    return config


@pytest.mark.anyio
async def test_writer_client_initialization_insecure(
    mock_spicedb_config: MagicMock,
) -> None:
    """Uses an explicit plaintext channel for a Docker-network endpoint."""
    writer = SpiceDBWriter(cfg=mock_spicedb_config)
    channel = MagicMock()
    client = MagicMock()
    with (
        patch(
            "galadril_vision.connectors.authz.spicedb.grpc.aio.insecure_channel",
            return_value=channel,
        ) as insecure_channel,
        patch(
            "galadril_vision.connectors.authz.spicedb.PermissionsServiceStub",
            return_value=client,
        ) as client_type,
    ):
        client = await writer._ensure_client()

    insecure_channel.assert_called_once_with("spicedb:50051")
    client_type.assert_called_once_with(channel)
    assert client is client_type.return_value


@pytest.mark.anyio
async def test_writer_client_initialization_secure(
    mock_spicedb_config: MagicMock,
) -> None:
    """Validates secure certificate negotiation for production cloud infrastructure routing."""
    mock_spicedb_config.endpoint = "spicedb.production.internal:443"
    mock_spicedb_config.insecure = False
    writer = SpiceDBWriter(cfg=mock_spicedb_config)
    channel = MagicMock()
    client = MagicMock()
    with (
        patch(
            "galadril_vision.connectors.authz.spicedb.grpc.aio.secure_channel",
            return_value=channel,
        ) as secure_channel,
        patch(
            "galadril_vision.connectors.authz.spicedb.PermissionsServiceStub",
            return_value=client,
        ) as client_type,
    ):
        client = await writer._ensure_client()

    sys.modules["grpcutil"].bearer_token_credentials.assert_called_once_with(
        "secret_grpc_token"
    )
    secure_channel.assert_called_once_with(
        "spicedb.production.internal:443",
        sys.modules["grpcutil"].bearer_token_credentials.return_value,
    )
    client_type.assert_called_once_with(channel)
    assert client is client_type.return_value


def test_split_reference_valid_and_invalid_formats(
    mock_spicedb_config: MagicMock,
) -> None:
    """Checks reference parser constraint enforcements for regular names and system IDs."""
    writer = SpiceDBWriter(
        cfg=mock_spicedb_config, subject_normalization_type="user"
    )

    obj_type, obj_id = writer._split_reference("workspace:12345", "resource")
    assert obj_type == "workspace"
    assert obj_id == "12345"

    fallback_type, fallback_id = writer._split_reference(
        "plain_username", "subject"
    )
    assert fallback_type == "user"
    assert fallback_id == "plain_username"

    with pytest.raises(
        TenantIsolationError, match="resource is missing object type"
    ):
        writer._split_reference("invalid_plain_resource", "resource")

    with pytest.raises(
        TenantIsolationError, match="resource object type is invalid"
    ):
        writer._split_reference("123_invalid_type:id", "resource")

    with pytest.raises(
        TenantIsolationError, match="resource object id is empty"
    ):
        writer._split_reference("workspace:", "resource")


def test_spicedb_object_ids_escape_unsupported_bytes_without_collisions() -> (
    None
):
    """Keeps arbitrary storage keys valid and injective at the SpiceDB edge."""
    assert (
        canonical_spicedb_object_id("tenant/raw/report.txt")
        == "tenant/raw/report=2Etxt"
    )
    assert (
        canonical_spicedb_object_id("tenant/raw/report=2Etxt")
        == "tenant/raw/report=3D2Etxt"
    )
    assert (
        canonical_spicedb_object_id("tenant/topic:record")
        == "tenant/topic=3Arecord"
    )


def test_validate_tuple_tenant_isolation_boundaries(
    mock_spicedb_config: MagicMock,
) -> None:
    """Enforces strict structural containment checks across tenant tenancy barriers."""
    writer = SpiceDBWriter(cfg=mock_spicedb_config)

    valid_tuple = AuthzTuple(
        tenant_id="tenant_alpha",
        resource="raw:tenant_alpha/doc_1",
        relation="reader",
        subject="user:bob",
    )

    with patch(
        "galadril_vision.connectors.authz.spicedb.require_same_tenant",
        return_value="tenant_alpha",
    ):
        assert (
            writer._validate_tuple("tenant_alpha", valid_tuple) is valid_tuple
        )

    invalid_relation_tuple = AuthzTuple(
        tenant_id="tenant_alpha",
        resource="document:tenant_alpha/doc_1",
        relation="invalid-relation-name!",
        subject="user:bob",
    )
    with patch(
        "galadril_vision.connectors.authz.spicedb.require_same_tenant",
        return_value="tenant_alpha",
    ):
        with pytest.raises(TenantIsolationError, match="relation is invalid"):
            writer._validate_tuple("tenant_alpha", invalid_relation_tuple)

    unscoped_resource_tuple = AuthzTuple(
        tenant_id="tenant_alpha",
        resource="document:tenant_beta/doc_1",
        relation="viewer",
        subject="user:bob",
    )
    with patch(
        "galadril_vision.connectors.authz.spicedb.require_same_tenant",
        return_value="tenant_alpha",
    ):
        with pytest.raises(
            TenantIsolationError,
            match="resource object id is not tenant scoped",
        ):
            writer._validate_tuple("tenant_alpha", unscoped_resource_tuple)


@pytest.mark.anyio
async def test_write_relationships_empty_and_populated(
    mock_spicedb_config: MagicMock,
) -> None:
    """Tests full relationship write sequencing including data formatting and gRPC dispatching."""
    writer = SpiceDBWriter(cfg=mock_spicedb_config)
    await writer.write_relationships("tenant_alpha", [])

    tuples = [
        AuthzTuple(
            tenant_id="tenant_alpha",
            resource="folder:tenant_alpha/f1",
            relation="owner",
            subject="user:alice#member",
        )
    ]

    mock_client = AsyncMock()
    with patch.object(writer, "_validate_tuple", return_value=tuples[0]):
        writer._client = mock_client
        await writer.write_relationships("tenant_alpha", tuples)
        _, kwargs = mock_client.WriteRelationships.call_args
        assert kwargs["metadata"] == (
            ("authorization", "Bearer secret_grpc_token"),
        )


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
