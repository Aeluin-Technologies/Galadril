"""Unit tests targeting the asynchronous PostgreSQL connection pool client."""

from unittest.mock import ANY, AsyncMock, MagicMock, patch

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
    config.maintenance_dsn = "postgresql://user:pass@localhost:5432/dbname"
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

    with patch.object(
        client, "_init_database_infrastructure", new_callable=AsyncMock
    ) as mock_init:
        await client.connect(initialize_database_infrastructure=True)
        mock_init.assert_called_once()

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

    with patch.object(
        PostgresClient,
        "_init_database_infrastructure",
        new_callable=AsyncMock,
    ):
        async with PostgresClient(config=mock_config) as client:
            assert client._pool is mock_pool

    mock_pool.close.assert_called_once()


@pytest.mark.asyncio
async def test_configure_pooled_connection_routines() -> None:
    """Ensures static connection hooks apply correct runtime extension environments and search paths."""
    mock_cursor = AsyncMock()
    mock_cursor.execute = AsyncMock()

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_conn.commit = AsyncMock()

    await PostgresClient._configure_pooled_connection(mock_conn)

    assert mock_cursor.execute.call_count == 2
    mock_cursor.execute.assert_any_call("LOAD 'age';")
    mock_cursor.execute.assert_any_call(
        "SET search_path = public, ag_catalog, '$user';"
    )
    mock_conn.commit.assert_called_once()


@pytest.mark.asyncio
@patch("galadril_vision.connectors.postgres.client.AsyncConnectionPool")
async def test_connect_infrastructure_failure_rollback(
    mock_pool_cls: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies that failures during schema loading trigger automatic pool closure and variable resets."""
    mock_pool = MagicMock()
    mock_pool.open = AsyncMock()
    mock_pool.close = AsyncMock()
    mock_pool_cls.return_value = mock_pool

    client = PostgresClient(config=mock_config)

    with patch.object(
        client,
        "_init_database_infrastructure",
        side_effect=RuntimeError("DDL crash"),
    ):
        with pytest.raises(RuntimeError, match="DDL crash"):
            await client.connect(initialize_database_infrastructure=True)

    mock_pool.close.assert_called_once()
    assert client._pool is None


@pytest.mark.asyncio
@patch("galadril_vision.connectors.postgres.client.create_async_engine")
@patch("galadril_vision.connectors.postgres.client.Base")
async def test_init_database_infrastructure_flow(
    mock_base: MagicMock, mock_create_engine: MagicMock, mock_config: MagicMock
) -> None:
    """Validates full table inspection modifications, extension provisioning loops, and graph checks."""
    mock_column = MagicMock()
    mock_column.name = "embedding"
    mock_column.type = MagicMock()
    mock_column.type.dimensions = 0

    mock_table = MagicMock()
    mock_table.columns = [mock_column]
    mock_base.metadata.tables = {"entity_embeddings": mock_table}

    mock_sa_conn = AsyncMock()
    mock_sa_conn.execute = AsyncMock()
    mock_sa_conn.run_sync = AsyncMock()

    mock_result = MagicMock()
    mock_result.fetchone.return_value = None
    mock_sa_conn.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.begin.return_value.__aenter__.return_value = mock_sa_conn
    mock_engine.dispose = AsyncMock()
    mock_create_engine.return_value = mock_engine

    client = PostgresClient(config=mock_config)
    await client._init_database_infrastructure()

    assert mock_column.type.dimensions == 128
    mock_create_engine.assert_called_once_with(
        "postgresql+psycopg://user:pass@localhost:5432/dbname"
    )

    mock_sa_conn.execute.assert_any_call(ANY)
    mock_sa_conn.run_sync.assert_called_once_with(mock_base.metadata.create_all)
    mock_engine.dispose.assert_called_once()


@pytest.mark.asyncio
async def test_connection_context_delivery_success(
    mock_config: MagicMock,
) -> None:
    """Verifies that connection pools yield active connection entities correctly under normal conditions."""
    client = PostgresClient(config=mock_config)
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    mock_conn.transaction = MagicMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn
    client._pool = mock_pool

    async with client.tenant_connection("tenant-test") as conn:
        assert conn is mock_conn
    mock_conn.execute.assert_awaited_once_with(
        "SELECT set_config('app.tenant_id', %s, true)",
        ("tenant-test",),
    )


@pytest.mark.asyncio
async def test_connection_context_uninitialized_error(
    mock_config: MagicMock,
) -> None:
    """Ensures a RuntimeError is thrown if an acquisition is requested prior to calling connect."""
    client = PostgresClient(config=mock_config)

    with pytest.raises(RuntimeError, match="Pool not initialized"):
        async with client.tenant_connection("tenant-test"):
            pass
