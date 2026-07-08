"""S3 client abstraction layer."""

from __future__ import annotations

from typing import Any

import aioboto3
import structlog
from botocore.config import Config

logger = structlog.get_logger(__name__)


class S3Client:
    """Manages an active S3 client session and network resources.

    Attributes:
        bucket: Default target S3 bucket name.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        aws_access_key: str | None = None,
        aws_secret_key: str | None = None,
        aws_region: str = "us-east-1",
    ) -> None:
        """Initializes the connection configuration.

        Args:
            bucket: Default target S3 bucket name.
            endpoint_url: Optional custom S3 endpoint URL.
            aws_access_key: Optional AWS access key ID.
            aws_secret_key: Optional AWS secret access key.
            aws_region: AWS region name. Defaults to "us-east-1".
        """
        self.bucket = bucket
        self._endpoint_url = endpoint_url
        self._aws_access_key = aws_access_key
        self._aws_secret_key = aws_secret_key
        self._aws_region = aws_region

        self._session = aioboto3.Session()
        self._client_context: Any = None
        self._client: Any = None

    async def __aenter__(self) -> S3Client:
        """Enters the asynchronous context and opens the client connection."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exits the asynchronous context and closes the client connection."""
        await self.close()

    async def connect(self) -> None:
        """Establishes the S3 client connection if not already connected."""
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

    async def list_object_keys(
        self, prefix: str, suffix: str = ".yaml"
    ) -> list[str]:
        """Lists matching configuration keys under the specified prefix.

        Args:
            prefix: S3 key prefix to filter objects.
            suffix: File extension to filter (e.g., '.yaml', '.parquet').

        Returns:
            A list of matching object keys.
        """
        await self.connect()
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []

        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if key and key.endswith(suffix):
                    keys.append(key)
        return keys

    async def get_object_bytes(
        self, key: str, target_bucket: str | None = None
    ) -> bytes:
        """Downloads and returns the raw bytes of an S3 object.

        Args:
            key: S3 object key to fetch.
            target_bucket: Optional bucket override. Defaults to default bucket.

        Returns:
            The raw bytes content of the object.
        """
        await self.connect()
        bucket_name = target_bucket or self.bucket
        response = await self._client.get_object(Bucket=bucket_name, Key=key)
        async with response["Body"] as stream:
            return await stream.read()

    async def get_object_with_metadata(
        self, key: str, target_bucket: str | None = None
    ) -> tuple[bytes, str | None]:
        """Downloads an S3 object and retrieves its Content-Type metadata.

        Args:
            key: S3 object key to fetch.
            target_bucket: Optional bucket override. Defaults to default bucket.

        Returns:
            A tuple containing the object bytes and the content type string.
        """
        await self.connect()
        bucket_name = target_bucket or self.bucket
        response = await self._client.get_object(Bucket=bucket_name, Key=key)
        mime_type = response.get("ContentType")
        async with response["Body"] as stream:
            content = await stream.read()
        return content, mime_type

    async def close(self) -> None:
        """Closes the underlying active S3 client context."""
        if self._client_context is not None:
            await self._client_context.__aexit__(None, None, None)
            self._client = None
            self._client_context = None
            logger.info("s3_client_connection_closed")
