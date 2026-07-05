"""Unit tests targeting stateful PostgreSQL engine allocation and loop cleanup structures."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import dagster as dg

from galadril_pipeline.resources.postgres import PostgresResource


def test_postgres_resource_uninitialized_access() -> None:
    """Ensures client access prior to execution framework setups raises runtime errors."""
    resource = PostgresResource(config_provider=MagicMock())
    with pytest.raises(
        RuntimeError, match="PostgresResource client accessed before setup."
    ):
        _ = resource.client


@patch("galadril_pipeline.resources.postgres.PostgresClient")
def test_postgres_resource_lifecycle_setup(mock_client_cls: MagicMock) -> None:
    """Validates target configurations map into execution connection engine states correctly."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_config_provider = MagicMock()
    mock_base_cfg = MagicMock()
    mock_config_provider.vision_config = mock_base_cfg

    resource = PostgresResource(config_provider=mock_config_provider)
    resource.setup_for_execution(mock_context)

    mock_client_cls.assert_called_once_with(mock_base_cfg.postgres)
    assert resource.client is mock_client_cls.return_value


@patch("galadril_pipeline.resources.postgres.PostgresClient")
@patch("asyncio.get_running_loop")
def test_postgres_resource_teardown_with_active_loop(
    mock_get_loop: MagicMock, mock_client_cls: MagicMock
) -> None:
    """Validates engine socket drops offload asynchronously within running event loop states."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_loop = MagicMock()
    mock_get_loop.return_value = mock_loop

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client_cls.return_value = mock_client

    resource = PostgresResource(config_provider=MagicMock())
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
    """Validates synchronous execution fallback paths close database sockets cleanly when event loops are missing."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_client = MagicMock()
    mock_client.close = MagicMock()
    mock_client_cls.return_value = mock_client

    resource = PostgresResource(config_provider=MagicMock())
    resource.setup_for_execution(mock_context)
    resource.teardown_after_execution(mock_context)

    mock_asyncio_run.assert_called_once()
