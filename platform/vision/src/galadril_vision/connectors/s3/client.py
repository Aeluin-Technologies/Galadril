"""S3 client abstraction layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import TracebackType
from typing import Protocol, cast

import aioboto3
import structlog
from botocore.config import Config

logger = structlog.get_logger(__name__)


class _AsyncBody(Protocol):
    async def __aenter__(self) -> _AsyncBody: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    async def read(self) -> bytes: ...


class _S3Paginator(Protocol):
    def paginate(
        self, *, Bucket: str, Prefix: str
    ) -> AsyncIterator[dict[str, object]]: ...


class _AsyncS3Client(Protocol):
    def get_paginator(self, operation_name: str) -> _S3Paginator: ...

    async def get_object(
        self, *, Bucket: str, Key: str
    ) -> dict[str, object]: ...


class _S3ClientContext(Protocol):
    async def __aenter__(self) -> _AsyncS3Client: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


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
        self._client_context: _S3ClientContext | None = None
        self._client: _AsyncS3Client | None = None

    async def __aenter__(self) -> S3Client:
        """Enters the asynchronous context and opens the client connection."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
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

        self._client_context = cast(
            _S3ClientContext,
            self._session.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._aws_access_key,
                aws_secret_access_key=self._aws_secret_key,
                config=boto_config,
            ),
        )
        self._client = await self._client_context.__aenter__()

    async def list_object_keys(
        self,
        prefix: str,
        suffix: str | tuple[str, ...] = (".yaml", ".yml"),
    ) -> list[str]:
        """Lists matching configuration keys under the specified prefix.

        Args:
            prefix: S3 key prefix to filter objects.
            suffix: File extension or immutable extension tuple to filter.

        Returns:
            A list of matching object keys.
        """
        await self.connect()
        if self._client is None:
            raise RuntimeError("S3 client initialization failed")
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []

        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            if not isinstance(contents, list):
                continue
            for obj in contents:
                if not isinstance(obj, dict):
                    continue
                key = obj.get("Key")
                if isinstance(key, str) and key.endswith(suffix):
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
        if self._client is None:
            raise RuntimeError("S3 client initialization failed")
        bucket_name = target_bucket or self.bucket
        response = await self._client.get_object(Bucket=bucket_name, Key=key)
        body = response.get("Body")
        if body is None:
            raise TypeError("S3 response is missing its body")
        async with cast(_AsyncBody, body) as stream:
            content = await stream.read()
        if not isinstance(content, bytes):
            raise TypeError("S3 object body must return bytes")
        return content

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
        if self._client is None:
            raise RuntimeError("S3 client initialization failed")
        bucket_name = target_bucket or self.bucket
        response = await self._client.get_object(Bucket=bucket_name, Key=key)
        raw_mime_type = response.get("ContentType")
        mime_type = raw_mime_type if isinstance(raw_mime_type, str) else None
        body = response.get("Body")
        if body is None:
            raise TypeError("S3 response is missing its body")
        async with cast(_AsyncBody, body) as stream:
            content = await stream.read()
        if not isinstance(content, bytes):
            raise TypeError("S3 object body must return bytes")
        return content, mime_type

    async def close(self) -> None:
        """Closes the underlying active S3 client context."""
        if self._client_context is not None:
            await self._client_context.__aexit__(None, None, None)
            self._client = None
            self._client_context = None
            logger.info("s3_client_connection_closed")
