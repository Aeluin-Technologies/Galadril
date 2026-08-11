"""Unit tests targeting the asynchronous S3 client layer."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from galadril_vision.connectors.s3.client import S3Client


@pytest.mark.asyncio
@patch("galadril_vision.connectors.s3.client.aioboto3.Session")
async def test_s3_client_lifecycle_and_connection(
    mock_session_cls: MagicMock,
) -> None:
    """Validates the asynchronous lifecycle connection routines and context managers."""
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    mock_client_context = AsyncMock()
    mock_client = MagicMock()
    mock_session.client.return_value = mock_client_context
    mock_client_context.__aenter__.return_value = mock_client

    client = S3Client(
        bucket="test-bucket", endpoint_url="http://localhost:4566"
    )
    await client.connect()

    assert client._client is mock_client
    mock_session.client.assert_called_once_with(
        "s3",
        endpoint_url="http://localhost:4566",
        aws_access_key_id=None,
        aws_secret_access_key=None,
        config=ANY,
    )

    await client.close()
    assert client._client is None
    mock_client_context.__aexit__.assert_called_once()

    mock_client_context.__aexit__.reset_mock()
    async with S3Client(bucket="test-bucket") as ctx_client:
        assert ctx_client._client is mock_client

    mock_client_context.__aexit__.assert_called_once()


@pytest.mark.asyncio
@patch("galadril_vision.connectors.s3.client.aioboto3.Session")
async def test_s3_client_list_object_keys_filtering(
    mock_session_cls: MagicMock,
) -> None:
    """Verifies that key listing aggregates and filters only .yaml and .yml files via the paginator."""
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    mock_client_context = AsyncMock()
    mock_client = MagicMock()
    mock_session.client.return_value = mock_client_context
    mock_client_context.__aenter__.return_value = mock_client

    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator

    async def mock_paginate(
        *args: Any, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "Contents": [
                {"Key": "configs/pipeline.yaml"},
                {"Key": "configs/readme.md"},
            ]
        }
        yield {
            "Contents": [
                {"Key": "configs/sub_config.yml"},
                {"Key": "configs/image.png"},
            ]
        }

    mock_paginator.paginate = mock_paginate

    client = S3Client(bucket="test-bucket")
    keys = await client.list_object_keys(prefix="configs/")

    assert keys == ["configs/pipeline.yaml", "configs/sub_config.yml"]


@pytest.mark.asyncio
@patch("galadril_vision.connectors.s3.client.aioboto3.Session")
async def test_s3_client_get_object_bytes_and_metadata(
    mock_session_cls: MagicMock,
) -> None:
    """Validates object retrieval pipelines, streaming blocks, and target bucket overrides."""
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    mock_client_context = AsyncMock()
    mock_client = MagicMock()
    mock_client.get_object = AsyncMock()
    mock_session.client.return_value = mock_client_context
    mock_client_context.__aenter__.return_value = mock_client

    mock_stream = AsyncMock()
    mock_stream.read.return_value = b"raw_file_payload"

    mock_body = AsyncMock()
    mock_body.__aenter__.return_value = mock_stream

    mock_client.get_object.return_value = {
        "Body": mock_body,
        "ContentType": "application/x-yaml",
    }

    client = S3Client(bucket="default-bucket")

    bytes_data = await client.get_object_bytes(key="manifest.yaml")
    assert bytes_data == b"raw_file_payload"
    mock_client.get_object.assert_called_with(
        Bucket="default-bucket", Key="manifest.yaml"
    )

    content, mime = await client.get_object_with_metadata(
        key="shared.yaml", target_bucket="custom-bucket"
    )
    assert content == b"raw_file_payload"
    assert mime == "application/x-yaml"
    mock_client.get_object.assert_called_with(
        Bucket="custom-bucket", Key="shared.yaml"
    )
