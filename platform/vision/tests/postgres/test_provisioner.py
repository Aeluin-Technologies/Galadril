"""Unit tests for privileged PostgreSQL infrastructure provisioning."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.common.config import (
    PostgresConnectorConfig,
    PostgresProvisioningConfig,
)
from galadril_vision.connectors.postgres.provisioner import PostgresProvisioner


def _runtime_config(*, user: str = "vision_runtime") -> PostgresConnectorConfig:
    """Builds an unprivileged runtime configuration fixture."""
    return PostgresConnectorConfig(
        database="vision",
        host="postgres:5432",
        user=user,
        password="runtime-secret",
        vector_dimensions=128,
        graph_name="vision_graph",
    )


def _admin_config(*, user: str = "vision_admin") -> PostgresProvisioningConfig:
    """Builds a privileged provisioning configuration fixture."""
    return PostgresProvisioningConfig(
        database="vision",
        host="postgres:5432",
        user=user,
        password="admin-secret",
    )


def test_provisioner_rejects_shared_role() -> None:
    """Prevents privileged credentials from becoming runtime credentials."""
    with pytest.raises(ValueError, match="must be different"):
        PostgresProvisioner(
            _admin_config(user="shared"), _runtime_config(user="shared")
        )


@pytest.mark.asyncio
@patch("galadril_vision.connectors.postgres.provisioner.create_async_engine")
async def test_provisioner_rejects_superuser_runtime_role(
    create_engine: MagicMock,
) -> None:
    """Prevents accidental use of a superuser runtime identity."""
    result = MagicMock()
    result.fetchone.return_value = (True,)
    conn = AsyncMock()
    conn.execute.return_value = result
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = conn
    engine.dispose = AsyncMock()
    create_engine.return_value = engine

    provisioner = PostgresProvisioner(_admin_config(), _runtime_config())
    with pytest.raises(ValueError, match="administrative attributes"):
        await provisioner.provision()

    conn.run_sync.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_provisioner_rejects_inherited_runtime_ddl() -> None:
    """Fails closed when PUBLIC or a parent role still grants object creation."""
    result = MagicMock()
    result.fetchone.return_value = (False, True, False)
    conn = AsyncMock()
    conn.execute.return_value = result
    provisioner = PostgresProvisioner(_admin_config(), _runtime_config())

    with pytest.raises(ValueError, match="inherited role grants"):
        await provisioner._validate_effective_runtime_privileges(conn)


@pytest.mark.asyncio
@patch("galadril_vision.connectors.postgres.provisioner.Base")
@patch("galadril_vision.connectors.postgres.provisioner.create_async_engine")
async def test_provisioner_uses_admin_dsn_and_grants_dml_only(
    create_engine: MagicMock, base: MagicMock
) -> None:
    """Validates explicit admin use and least-privilege runtime grants."""
    role_result = MagicMock()
    role_result.fetchone.return_value = (False,)
    graph_result = MagicMock()
    graph_result.fetchone.return_value = (1,)
    privilege_result = MagicMock()
    privilege_result.fetchone.return_value = (False, False, False)
    generic_result = MagicMock()

    def execute_result(statement: object, *_args: object) -> MagicMock:
        """Returns deterministic rows for catalog checks."""
        rendered = str(statement)
        if "pg_catalog.pg_roles" in rendered:
            return role_result
        if "ag_catalog.ag_graph" in rendered:
            return graph_result
        if "has_database_privilege" in rendered:
            return privilege_result
        return generic_result

    conn = AsyncMock()
    conn.execute.side_effect = execute_result
    conn.run_sync = AsyncMock()
    conn.dialect = MagicMock()
    conn.dialect.identifier_preparer.quote_identifier.side_effect = (
        lambda value: f'"{value}"'
    )
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = conn
    engine.dispose = AsyncMock()
    create_engine.return_value = engine

    embedding = MagicMock()
    embedding.name = "embedding"
    embedding.type.dimensions = 0
    base.metadata.tables = {
        "entity_embeddings": MagicMock(columns=[embedding])
    }

    await PostgresProvisioner(_admin_config(), _runtime_config()).provision()

    create_engine.assert_called_once_with(
        "postgresql+psycopg://vision_admin:admin-secret@postgres:5432/vision"
    )
    conn.run_sync.assert_awaited_once_with(base.metadata.create_all)
    statements = "\n".join(
        str(call.args[0]) for call in conn.execute.await_args_list
    )
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in statements
    assert "GRANT CREATE" not in statements
    assert "REVOKE CREATE ON DATABASE" in statements
    assert "REVOKE CREATE ON SCHEMA" in statements
    assert "SET session_preload_libraries = 'age'" in statements
    assert 'TO "vision_runtime"' in statements
    engine.dispose.assert_awaited_once()
