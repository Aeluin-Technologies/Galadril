"""Unit tests targeting the asynchronous PostgreSQL connection pool client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.connectors.postgres.client import PostgresClient


class FakeDSN:
    """Stub to simulate a DSN object converting to string."""

    def __str__(self) -> str:
        return "postgresql://user:pass@localhost:5432/dbname"


@pytest.fixture
def mock_config() -> MagicMock:
    """Provides a mocked configuration object for the Postgres connector."""
    config = MagicMock()
    config.dsn = FakeDSN()
    config.min_connections = 5
    config.max_connections = 20
    config.vector_dimensions = 128
    config.graph_name = "test_graph"
    return config


@pytest.mark.asyncio
@patch("galadril_vision.connectors.postgres.client.AsyncConnectionPool")
async def test_postgres_client_lifecycle_and_context(
    mock_pool_cls: MagicMock, mock_config: MagicMock
) -> None:
    """Validates the standard initialization, explicit connect, and connection pool shutdown."""
    mock_pool = MagicMock()
    mock_pool.open = AsyncMock()
    mock_pool.close = AsyncMock()
    mock_pool_cls.return_value = mock_pool

    client = PostgresClient(config=mock_config)

    await client.connect()

    assert client._pool is mock_pool
    mock_pool_cls.assert_called_once_with(
        conninfo="postgresql://user:pass@localhost:5432/dbname",
        min_size=5,
        max_size=20,
        open=False,
        configure=client._configure_pooled_connection,
    )
    mock_pool.open.assert_called_once()

    await client.connect()
    assert mock_pool_cls.call_count == 1

    await client.close()
    mock_pool.close.assert_called_once()
    assert client._pool is None


@pytest.mark.asyncio
@patch("galadril_vision.connectors.postgres.client.AsyncConnectionPool")
async def test_postgres_client_async_context_manager(
    mock_pool_cls: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies correct behavior when wrapping the client inside an async context block."""
    mock_pool = MagicMock()
    mock_pool.open = AsyncMock()
    mock_pool.close = AsyncMock()
    mock_pool_cls.return_value = mock_pool

    async with PostgresClient(config=mock_config) as client:
        assert client._pool is mock_pool

    mock_pool.close.assert_called_once()


@pytest.mark.asyncio
async def test_configure_pooled_connection_routines(
    mock_config: MagicMock,
) -> None:
    """Ensures connection hooks enforce runtime privileges and search paths."""
    mock_cursor = AsyncMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone.return_value = (
        False,
        False,
        False,
        False,
        False,
        False,
    )

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_conn.commit = AsyncMock()

    client = PostgresClient(config=mock_config)
    await client._configure_pooled_connection(mock_conn)

    assert mock_cursor.execute.await_count == 2
    mock_cursor.execute.assert_any_await(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, "
        "has_database_privilege(current_user, current_database(), 'CREATE'), "
        "has_schema_privilege(current_user, 'public', 'CREATE'), "
        "has_schema_privilege(current_user, %s, 'CREATE') "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user",
        ("test_graph",),
    )
    mock_cursor.execute.assert_any_await(
        "SET search_path = public, ag_catalog, '$user';"
    )
    mock_conn.commit.assert_called_once()


@pytest.mark.asyncio
async def test_configure_pooled_connection_rejects_superuser(
    mock_config: MagicMock,
) -> None:
    """Ensures runtime pools cannot start with privileged identities."""
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = (True,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_conn.commit = AsyncMock()

    client = PostgresClient(config=mock_config)
    with pytest.raises(PermissionError, match="must not have DDL privileges"):
        await client._configure_pooled_connection(mock_conn)

    mock_conn.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_context_delivery_success(
    mock_config: MagicMock,
) -> None:
    """Verifies that connection pools yield active connection entities correctly under normal conditions."""
    client = PostgresClient(config=mock_config)
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn
    client._pool = mock_pool

    async with client.connection() as conn:
        assert conn is mock_conn


@pytest.mark.asyncio
async def test_connection_context_uninitialized_error(
    mock_config: MagicMock,
) -> None:
    """Ensures a RuntimeError is thrown if an acquisition is requested prior to calling connect."""
    client = PostgresClient(config=mock_config)

    with pytest.raises(RuntimeError, match="Pool not initialized"):
        async with client.connection():
            pass
