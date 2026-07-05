"""Unit tests targeting persistent storage framework allocation and lifecycle boundaries."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import dagster as dg
from galadril_pipeline.resources.s3 import S3ClientResource


def test_s3_resource_uninitialized_access() -> None:
    """Ensures uninitialized client access attempts raise accessible runtime safety faults."""
    resource = S3ClientResource(config_provider=MagicMock())
    with pytest.raises(
        RuntimeError, match="S3ClientResource client accessed before setup."
    ):
        _ = resource.client


@patch("galadril_pipeline.resources.s3.S3Client")
def test_s3_resource_lifecycle_setup(mock_client_cls: MagicMock) -> None:
    """Validates that underlying S3 storage references mirror credential configurations cleanly."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_config_provider = MagicMock()
    mock_s3_cfg = MagicMock()
    mock_s3_cfg.staging_bucket = "test-bucket"
    mock_s3_cfg.endpoint = "https://s3.local"
    mock_s3_cfg.access_key = "key"
    mock_s3_cfg.secret_key = "secret"
    mock_s3_cfg.region = "us-east-1"
    mock_config_provider.vision_config.connectors.s3 = mock_s3_cfg

    resource = S3ClientResource(config_provider=mock_config_provider)
    resource.setup_for_execution(mock_context)

    mock_client_cls.assert_called_once_with(
        bucket="test-bucket",
        endpoint_url="https://s3.local",
        aws_access_key="key",
        aws_secret_key="secret",
        aws_region="us-east-1",
    )
    assert resource.client is mock_client_cls.return_value


@patch("galadril_pipeline.resources.s3.S3Client")
@patch("asyncio.get_running_loop")
def test_s3_resource_teardown_with_active_loop(
    mock_get_loop: MagicMock, mock_client_cls: MagicMock
) -> None:
    """Validates structural context drops push task updates cleanly inside active network topologies."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_loop = MagicMock()
    mock_get_loop.return_value = mock_loop

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client_cls.return_value = mock_client

    resource = S3ClientResource(config_provider=MagicMock())
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
    """Ensures fallback orchestration loops trigger connection failures gracefully without active engines."""
    mock_context = MagicMock(spec=dg.InitResourceContext)
    mock_client = MagicMock()
    mock_client.close = MagicMock()
    mock_client_cls.return_value = mock_client

    resource = S3ClientResource(config_provider=MagicMock())
    resource.setup_for_execution(mock_context)
    resource.teardown_after_execution(mock_context)

    mock_asyncio_run.assert_called_once()
