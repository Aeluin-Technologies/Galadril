"""Daft Worker for concurrent data downloading."""

from __future__ import annotations

import asyncio
from typing import Any

import daft
import structlog

from galadril_vision.compute.helpers import (
    _build_raw_data_record,
    _decode_raw_content,
    _extract_text_payload,
    _infer_modality,
    _storage_location,
)
from galadril_vision.connectors.s3.client import S3Client

logger = structlog.get_logger(__name__)


@daft.func(return_dtype=daft.DataType.python())
class DownloadDataWorker:
    """Stateful worker pool for concurrent."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        endpoint_url: str | None,
        region_name: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        """Initializes connection credentials once per distributed cluster worker vcpu."""
        self.bucket = bucket
        self.prefix = prefix
        self.endpoint_url = endpoint_url
        self.region_name = region_name or "us-east-1"
        self.access_key = access_key
        self.secret_key = secret_key

        # Shared connection state across row tasks inside the same process.
        self.client: S3Client | None = None
        self._init_task: asyncio.Task[S3Client] | None = None
        self.inline_text_count = 0
        self.s3_download_count = 0
        self.failed_count = 0
        self.total_bytes_transferred = 0

    async def _init_client(self) -> S3Client:
        """Helper method to instantiate and establish the S3 client connection."""
        client = S3Client(
            bucket=self.bucket,
            endpoint_url=self.endpoint_url,
            aws_access_key=self.access_key,
            aws_secret_key=self.secret_key,
            aws_region=self.region_name,
        )
        await client.connect()
        return client

    async def __call__(
        self,
        storage_path: Any,
        record_id: Any,
        raw_payload: Any,
        metadata: Any,
    ) -> dict[str, Any] | None:
        """Executes concurrent row-wise downloads using Daft's native driving event loop."""

        if self._init_task is None:
            self._init_task = asyncio.create_task(self._init_client())

        try:
            self.client = await self._init_task
        except Exception:
            self._init_task = None
            raise

        modality = _infer_modality(storage_path, raw_payload, metadata)
        mime_type = None
        for container in (metadata, raw_payload):
            if isinstance(container, dict):
                mime_type = (
                    container.get("mime_type")
                    or container.get("content_type")
                    or mime_type
                )

        inline_text = _extract_text_payload(raw_payload)
        if inline_text is not None:
            self.inline_text_count += 1
            payload_size = len(
                str(inline_text).encode("utf-8", errors="ignore")
            )
            self.total_bytes_transferred += payload_size

            logger.debug(
                "processing_inline_payload",
                record_id=record_id,
                modality=modality,
                size_bytes=payload_size,
            )
            return _build_raw_data_record(
                record_id=record_id,
                storage_path=storage_path,
                raw_payload=raw_payload,
                metadata=metadata,
                content=inline_text,
                modality="text" if modality == "data" else modality,
                mime_type=mime_type or "text/plain",
            )

        if not storage_path:
            logger.warning(
                "missing_storage_path_and_inline_payload", record_id=record_id
            )
            self.failed_count += 1
            return None

        try:
            s3_bucket, key = _storage_location(
                str(storage_path), self.bucket, self.prefix
            )

            logger.debug(
                "s3_object_fetch_start",
                record_id=record_id,
                bucket=s3_bucket,
                key=key,
            )

            (
                content,
                effective_mime,
            ) = await self.client.get_object_with_metadata(
                key, target_bucket=s3_bucket
            )

            content_size = len(content) if content else 0
            self.s3_download_count += 1
            self.total_bytes_transferred += content_size

            logger.debug(
                "s3_object_fetch_success",
                record_id=record_id,
                size_bytes=content_size,
                resolved_mime=effective_mime,
            )

            effective_mime = effective_mime or mime_type
            data = _decode_raw_content(
                content, modality, effective_mime, record_id
            )

            return _build_raw_data_record(
                record_id=record_id,
                storage_path=storage_path,
                raw_payload=raw_payload,
                metadata=metadata,
                content=data,
                modality=modality,
                mime_type=effective_mime,
            )
        except Exception as exc:
            self.failed_count += 1
            logger.warning(
                "raw_data_load_failed",
                record_id=record_id,
                storage_path=storage_path,
                error=str(exc),
            )
            return None
