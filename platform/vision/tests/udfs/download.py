"""Unit tests for the data download worker implementation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.compute.udfs.download import DownloadDataWorker


class TestDownloadDataWorker:
    """Systematically evaluates download workers across network scenarios."""

    @pytest.mark.asyncio
    async def test_initialization_defaults(self) -> None:
        """Verifies default values and parameter mappings during constructor setup."""
        worker = DownloadDataWorker(
            bucket="test-bucket",
            prefix="test-prefix",
            endpoint_url="http://localhost:4566",
        )
        assert worker.bucket == "test-bucket"
        assert worker.prefix == "test-prefix"
        assert worker.endpoint_url == "http://localhost:4566"
        assert worker.region_name == "us-east-1"
        assert worker.client is None
        assert worker._init_task is None
        assert worker.inline_text_count == 0
        assert worker.s3_download_count == 0
        assert worker.failed_count == 0
        assert worker.total_bytes_transferred == 0

    @pytest.mark.asyncio
    async def test_init_client(self) -> None:
        """Validates proper initialization and connection orchestration of the S3 client."""
        worker = DownloadDataWorker(
            bucket="b",
            prefix="p",
            endpoint_url="e",
            region_name="us-west-2",
            access_key="ak",
            secret_key="sk",
        )
        with patch(
            "galadril_vision.compute.udfs.download.S3Client"
        ) as mock_s3_class:
            mock_client_instance = MagicMock()
            mock_client_instance.connect = AsyncMock()
            mock_s3_class.return_value = mock_client_instance

            client = await worker._init_client()
            assert client == mock_client_instance
            mock_s3_class.assert_called_once_with(
                bucket="b",
                endpoint_url="e",
                aws_access_key="ak",
                aws_secret_key="sk",
                aws_region="us-west-2",
            )
            mock_client_instance.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_client_initialization_failure(self) -> None:
        """Ensures initialization loop resets and handles setup errors gracefully."""
        worker = DownloadDataWorker(bucket="b", prefix="p", endpoint_url="e")
        with patch.object(
            worker, "_init_client", side_effect=ValueError("S3 Fail")
        ):
            with pytest.raises(ValueError, match="S3 Fail"):
                await worker("path", "id", {}, {})
            assert worker._init_task is None

    @pytest.mark.asyncio
    async def test_call_inline_text_payload(self) -> None:
        """Validates payload generation metrics when using inline text payloads."""
        worker = DownloadDataWorker(bucket="b", prefix="p", endpoint_url="e")
        worker._init_task = AsyncMock()

        payload = {"text": "hello world"}
        metadata = {"mime_type": "text/plain"}

        with (
            patch(
                "galadril_vision.compute.udfs.download._infer_modality",
                return_value="data",
            ),
            patch(
                "galadril_vision.compute.udfs.download._extract_text_payload",
                return_value="hello world",
            ),
            patch(
                "galadril_vision.compute.udfs.download._build_raw_data_record",
                return_value={"record_id": "id"},
            ) as mock_build,
        ):
            result = await worker("path", "id", payload, metadata)
            assert result == {"record_id": "id"}
            assert worker.inline_text_count == 1
            assert worker.total_bytes_transferred == 11
            mock_build.assert_called_once_with(
                record_id="id",
                storage_path="path",
                raw_payload=payload,
                metadata=metadata,
                content="hello world",
                modality="text",
                mime_type="text/plain",
            )

    @pytest.mark.asyncio
    async def test_call_missing_storage_path_and_inline(self) -> None:
        """Verifies failure tracking when storage paths and inline text are missing."""
        worker = DownloadDataWorker(bucket="b", prefix="p", endpoint_url="e")
        worker._init_task = AsyncMock()

        with (
            patch(
                "galadril_vision.compute.udfs.download._infer_modality",
                return_value="data",
            ),
            patch(
                "galadril_vision.compute.udfs.download._extract_text_payload",
                return_value=None,
            ),
        ):
            result = await worker("", "id", {}, {})
            assert result is None
            assert worker.failed_count == 1

    @pytest.mark.asyncio
    async def test_call_successful_s3_download(self) -> None:
        """Tests successful path downloads and payload conversion processes."""
        worker = DownloadDataWorker(bucket="b", prefix="p", endpoint_url="e")
        mock_client = AsyncMock()
        mock_client.get_object_with_metadata = AsyncMock(
            return_value=(b"rawbytes", "image/png")
        )
        worker._init_task = AsyncMock()
        worker._init_task.to_awaitable = AsyncMock(return_value=mock_client)
        worker.client = mock_client

        async def dummy_await():
            return mock_client

        worker._init_task.__await__ = dummy_await

        with (
            patch(
                "galadril_vision.compute.udfs.download._infer_modality",
                return_value="image",
            ),
            patch(
                "galadril_vision.compute.udfs.download._extract_text_payload",
                return_value=None,
            ),
            patch(
                "galadril_vision.compute.udfs.download._storage_location",
                return_value=("b", "p/path"),
            ),
            patch(
                "galadril_vision.compute.udfs.download._decode_raw_content",
                return_value="decoded_image",
            ),
            patch(
                "galadril_vision.compute.udfs.download._build_raw_data_record",
                return_value={"record_id": "id"},
            ) as mock_build,
        ):
            result = await worker("path", "id", {}, {})
            assert result == {"record_id": "id"}
            assert worker.s3_download_count == 1
            assert worker.total_bytes_transferred == 8
            mock_build.assert_called_once_with(
                record_id="id",
                storage_path="path",
                raw_payload={},
                metadata={},
                content="decoded_image",
                modality="image",
                mime_type="image/png",
            )

    @pytest.mark.asyncio
    async def test_call_s3_download_exception(self) -> None:
        """Validates tracking metrics when remote connections error out."""
        worker = DownloadDataWorker(bucket="b", prefix="p", endpoint_url="e")
        mock_client = AsyncMock()
        mock_client.get_object_with_metadata.side_effect = RuntimeError(
            "S3 Network Error"
        )

        async def dummy_await():
            return mock_client

        worker._init_task = MagicMock()
        worker._init_task.__await__ = dummy_await

        with (
            patch(
                "galadril_vision.compute.udfs.download._infer_modality",
                return_value="image",
            ),
            patch(
                "galadril_vision.compute.udfs.download._extract_text_payload",
                return_value=None,
            ),
            patch(
                "galadril_vision.compute.udfs.download._storage_location",
                return_value=("b", "p/path"),
            ),
        ):
            result = await worker("path", "id", {}, {})
            assert result is None
            assert worker.failed_count == 1
