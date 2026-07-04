"""Unit tests targeting the S3 client layer and transit serialization pipelines."""

import pytest
from typing import Any, AsyncGenerator, cast
from unittest.mock import AsyncMock, MagicMock, patch, ANY

import pyarrow.parquet as pq

from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.connectors.s3.client import S3Client
from galadril_vision.connectors.s3.transit import S3TransitService


class DummyCanonicalRecord:
    """Stub mimicking a Pydantic Model with a model_dump capabilities."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def model_dump(self) -> dict[str, Any]:
        return self.data


@pytest.mark.asyncio
@patch("galadril_vision.connectors.s3.client.aioboto3.Session")
async def test_s3_client_lifecycle_and_connection(
    mock_session_cls: MagicMock,
) -> None:
    """Validates the asynchronous lifecycle connection routines and context managers."""
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    mock_client_context = AsyncMock()
    mock_client = AsyncMock()
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
    mock_client = AsyncMock()
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
    mock_client = AsyncMock()
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


@pytest.mark.asyncio
async def test_s3_transit_service_upload_empty_records_error() -> None:
    """Ensures that uploading empty record sequences triggers an immediate ValueError."""
    mock_s3 = MagicMock()
    transit_service = S3TransitService(s3_client=mock_s3)

    with pytest.raises(
        ValueError, match="Cannot upload an empty list of records"
    ):
        await transit_service.upload([])


@pytest.mark.asyncio
async def test_s3_transit_service_upload_success() -> None:
    """Validates successful Arrow/Parquet serialization and multi-part upload invocation."""
    mock_s3_client = AsyncMock()

    mock_s3 = MagicMock()
    mock_s3.bucket = "transit-bucket"
    mock_s3.connect = AsyncMock()
    mock_s3._client = mock_s3_client

    transit_service = S3TransitService(s3_client=mock_s3)

    records = [
        DummyCanonicalRecord({"entity_id": "e1", "score": 0.95}),
        DummyCanonicalRecord({"entity_id": "e2", "score": 0.88}),
    ]

    uri = await transit_service.upload(cast(list[CanonicalRecord], records))

    assert uri.startswith("s3://transit-bucket/staging/micro_batches/")
    assert uri.endswith(".parquet")

    mock_s3.connect.assert_called_once()
    mock_s3_client.put_object.assert_called_once_with(
        Bucket="transit-bucket",
        Key=ANY,
        Body=ANY,
        ContentType="application/parquet",
    )

    call_kwargs = mock_s3_client.put_object.call_args[1]
    sent_buffer = call_kwargs["Body"]

    sent_buffer.seek(0)
    table = pq.read_table(sent_buffer)
    assert table.num_rows == 2
    assert table.column("entity_id").to_pylist() == ["e1", "e2"]
    assert table.column("score").to_pylist() == [0.95, 0.88]


@pytest.mark.asyncio
async def test_s3_transit_service_serialization_failure_handling() -> None:
    """Ensures exceptions raised during PyArrow table transformation are captured and re-raised."""
    mock_s3 = MagicMock()
    mock_s3.bucket = "transit-bucket"
    mock_s3.connect = AsyncMock()

    transit_service = S3TransitService(s3_client=mock_s3)

    malformed_records = [
        DummyCanonicalRecord({"field": "string_type"}),
        DummyCanonicalRecord({"field": 12345}),
    ]

    with patch(
        "galadril_vision.connectors.s3.transit.pa.Table.from_pylist",
        side_effect=TypeError("Arrow Type Error"),
    ):
        with pytest.raises(TypeError, match="Arrow Type Error"):
            await transit_service.upload(
                cast(list[CanonicalRecord], malformed_records)
            )


@pytest.mark.asyncio
async def test_s3_transit_service_upload_network_failure_handling() -> None:
    """Ensures exceptions raised during put_object operations are gracefully logged and propagated."""
    mock_s3_client = AsyncMock()
    mock_s3_client.put_object.side_effect = Exception("S3 API Network Error")

    mock_s3 = MagicMock()
    mock_s3.bucket = "transit-bucket"
    mock_s3.connect = AsyncMock()
    mock_s3._client = mock_s3_client

    transit_service = S3TransitService(s3_client=mock_s3)
    records = [DummyCanonicalRecord({"event": "test"})]

    with pytest.raises(Exception, match="S3 API Network Error"):
        await transit_service.upload(cast(list[CanonicalRecord], records))
