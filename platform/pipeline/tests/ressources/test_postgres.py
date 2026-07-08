"""Unit tests targeting stateful PostgreSQL engine allocation and loop cleanup structures."""

from unittest.mock import AsyncMock, MagicMock, patch

import dagster as dg
import pytest
from galadril_pipeline.resources.postgres import PostgresResource


def test_postgres_resource_uninitialized_access() -> None:
    """Ensures client proxy access rules raise accessible runtime faults before setup blocks execute."""
    resource = PostgresResource(
        host="localhost",
        port=5432,
        username="root",
        password="pwd",
        database="prod",
    )
    with pytest.raises(
        RuntimeError, match="PostgresResource client accessed before setup."
    ):
        _ = resource.client


@patch("galadril_pipeline.resources.postgres.PostgresClient")
@patch("galadril_pipeline.resources.postgres.PostgresConnectorConfig")
def test_postgres_resource_lifecycle_setup(
    mock_config_cls: MagicMock, mock_client_cls: MagicMock
) -> None:
    """Validates target resource variables compile into correct engine configuration specs."""
    mock_context = MagicMock(spec=dg.InitResourceContext)

    resource = PostgresResource(
        host="127.0.0.1",
        port=5432,
        username="galadril_user",
        password="secure_password",
        database="vision_warehouse",
    )

    resource.setup_for_execution(mock_context)

    mock_config_cls.assert_called_once_with(
        host="127.0.0.1:5432",
        user="galadril_user",
        password="secure_password",
        database="vision_warehouse",
    )
    mock_client_cls.assert_called_once_with(config=mock_config_cls.return_value)
    assert resource.client is mock_client_cls.return_value


@patch("galadril_pipeline.resources.postgres.PostgresClient")
@patch("asyncio.get_running_loop")
def test_postgres_resource_teardown_with_active_loop(
    mock_get_loop: MagicMock, mock_client_cls: MagicMock
) -> None:
    """Validates engine socket drops offload safely inside active running event loop task queues."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_loop = MagicMock()
    mock_get_loop.return_value = mock_loop

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client_cls.return_value = mock_client

    resource = PostgresResource(
        host="h", port=5432, username="u", password="p", database="d"
    )
    resource.setup_for_execution(mock_context)
    resource.teardown_after_execution(mock_context)

    mock_get_loop.assert_called_once()
    mock_loop.create_task.assert_called_once()


@patch("galadril_pipeline.resources.postgres.PostgresClient")
@patch("asyncio.get_running_loop", side_effect=RuntimeError("No loop running"))
@patch("asyncio.run")
def test_postgres_resource_teardown_without_active_loop(
    mock_asyncio_run: MagicMock,
    mock_get_loop: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    """Validates synchronous fallback executions invoke closing database connections when loops are absent."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_client = MagicMock()
    mock_client.close = MagicMock()
    mock_client_cls.return_value = mock_client

    resource = PostgresResource(
        host="h", port=5432, username="u", password="p", database="d"
    )
    resource.setup_for_execution(mock_context)
    resource.teardown_after_execution(mock_context)

    mock_asyncio_run.assert_called_once()
