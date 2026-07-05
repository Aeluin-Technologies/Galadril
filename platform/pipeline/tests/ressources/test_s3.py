"""Unit tests targeting persistent storage framework allocation and lifecycle boundaries."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import dagster as dg

from galadril_pipeline.resources.s3 import S3ClientResource


def test_s3_resource_uninitialized_access() -> None:
    """Ensures uninitialized client property calls raise explicit structural validation faults."""
    resource = S3ClientResource(
        bucket="b",
        endpoint_url="url",
        aws_access_key="k",
        aws_secret_key="s",
        aws_region="r",
    )
    with pytest.raises(
        RuntimeError, match="S3ClientResource client accessed before setup."
    ):
        _ = resource.client


@patch("galadril_pipeline.resources.s3.S3Client")
def test_s3_resource_lifecycle_setup(mock_client_cls: MagicMock) -> None:
    """Validates that underlying S3 storage connection variables map accurately to fields."""
    mock_context = MagicMock(spec=dg.InitResourceContext)

    resource = S3ClientResource(
        bucket="galadril-staging-bucket",
        endpoint_url="https://minio.local:9000",
        aws_access_key="admin_key",
        aws_secret_key="super_secret_key",
        aws_region="us-west-2",
    )

    resource.setup_for_execution(mock_context)

    mock_client_cls.assert_called_once_with(
        bucket="galadril-staging-bucket",
        endpoint_url="https://minio.local:9000",
        aws_access_key="admin_key",
        aws_secret_key="super_secret_key",
        aws_region="us-west-2",
    )
    assert resource.client is mock_client_cls.return_value


@patch("galadril_pipeline.resources.s3.S3Client")
@patch("asyncio.get_running_loop")
def test_s3_resource_teardown_with_active_loop(
    mock_get_loop: MagicMock, mock_client_cls: MagicMock
) -> None:
    """Validates network closing blocks schedule execution correctly within running event loops."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_loop = MagicMock()
    mock_get_loop.return_value = mock_loop

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client_cls.return_value = mock_client

    resource = S3ClientResource(
        bucket="b",
        endpoint_url="url",
        aws_access_key="k",
        aws_secret_key="s",
        aws_region="r",
    )
    resource.setup_for_execution(mock_context)
    resource.teardown_after_execution(mock_context)

    mock_loop.create_task.assert_called_once()


@patch("galadril_pipeline.resources.s3.S3Client")
@patch("asyncio.get_running_loop", side_effect=RuntimeError("No loop running"))
@patch("asyncio.run")
def test_s3_resource_teardown_without_active_loop(
    mock_asyncio_run: MagicMock,
    mock_get_loop: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    """Ensures synchronous fallbacks close storage sockets cleanly without active engine frameworks."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_client = MagicMock()
    mock_client.close = MagicMock()
    mock_client_cls.return_value = mock_client

    resource = S3ClientResource(
        bucket="b",
        endpoint_url="url",
        aws_access_key="k",
        aws_secret_key="s",
        aws_region="r",
    )
    resource.setup_for_execution(mock_context)
    resource.teardown_after_execution(mock_context)

    mock_asyncio_run.assert_called_once()
