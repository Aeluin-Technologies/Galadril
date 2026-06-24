"""Asynchronous S3 connection client abstraction layer built on top of aioboto3."""

from __future__ import annotations

from typing import Any, Optional
import aioboto3
from botocore.config import Config
import structlog

logger = structlog.get_logger(__name__)


class S3Client:
    """Non-blocking, lifecycle-aware S3 client connector with optimized pooling."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: Optional[str] = None,
        aws_access_key: Optional[str] = None,
        aws_secret_key: Optional[str] = None,
        aws_region: str = "us-east-1",
    ) -> None:
        """Initializes client configurations without triggering immediate I/O operations."""
        self.bucket = bucket
        self._endpoint_url = endpoint_url
        self._aws_access_key = aws_access_key
        self._aws_secret_key = aws_secret_key
        self._aws_region = aws_region

        self._session = aioboto3.Session()
        self._client_context: Any = None
        self._client: Any = None

    async def __aenter__(self) -> S3Client:
        """Asynchronous context manager hook for deterministic resource allocation."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Guarantees underlying pooling resource cleanup on block exit."""
        await self.close()

    async def connect(self) -> None:
        """Idempotently spins up the non-blocking client context."""
        if self._client is not None:
            return

        boto_config = Config(
            region_name=self._aws_region,
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            max_pool_connections=50,
        )

        self._client_context = self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._aws_access_key,
            aws_secret_access_key=self._aws_secret_key,
            config=boto_config,
        )
        self._client = await self._client_context.__aenter__()

    async def list_object_keys(self, prefix: str) -> list[str]:
        """Lists and aggregates matching S3 object keys iteratively via async pagination."""
        await self.connect()
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []

        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if key and (key.endswith(".yaml") or key.endswith(".yml")):
                    keys.append(key)
        return keys

    async def get_object_bytes(
        self, key: str, target_bucket: Optional[str] = None
    ) -> bytes:
        """Downloads the target S3 object content fully into memory as byte arrays."""
        await self.connect()
        bucket_name = target_bucket or self.bucket
        response = await self._client.get_object(Bucket=bucket_name, Key=key)
        async with response["Body"] as stream:
            return await stream.read()

    async def get_object_with_metadata(
        self, key: str, target_bucket: Optional[str] = None
    ) -> tuple[bytes, Optional[str]]:
        """Downloads an object and extracts its content type header."""
        await self.connect()
        bucket_name = target_bucket or self.bucket
        response = await self._client.get_object(Bucket=bucket_name, Key=key)
        mime_type = response.get("ContentType")
        async with response["Body"] as stream:
            content = await stream.read()
        return content, mime_type

    async def close(self) -> None:
        """Gracefully closes open network channels and descriptors."""
        if self._client_context is not None:
            await self._client_context.__aexit__(None, None, None)
            self._client = None
            self._client_context = None
            logger.info("s3_client_connection_closed")
