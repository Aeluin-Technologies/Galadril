"""S3 artifact loader with local caching."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aioboto3
import structlog

from galadril_inference.common.exceptions import ArtifactResolutionError
from galadril_inference.loading.loader import ArtifactLoader

logger = structlog.get_logger(__name__)

_DEFAULT_CACHE_DIR = Path(tempfile.gettempdir()) / "galadril_artifact_cache"


class S3Loader(ArtifactLoader):
    """Downloads and caches model artifacts from Amazon S3."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        cache_dir: str | Path | None = None,
        endpoint_url: str | None = None,
        aws_access_key: str | None = None,
        aws_secret_key: str | None = None,
        aws_region: str = "us-east-1",
    ) -> None:
        """Initializes the loader.

        Args:
            bucket: Name of the S3 bucket.
            prefix: Root prefix key in the S3 bucket.
            cache_dir: Optional local directory path for cached files.
            endpoint_url: Optional custom S3 endpoint URL.
            aws_access_key: Optional AWS access key ID.
            aws_secret_key: Optional AWS secret access key.
            aws_region: AWS region name. Defaults to "us-east-1".
        """
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._cache_dir = Path(
            cache_dir
            or os.environ.get(
                "GALADRIL_ARTIFACT_CACHE", str(_DEFAULT_CACHE_DIR)
            )
        ).resolve()

        self._endpoint_url = endpoint_url
        self._aws_access_key = aws_access_key
        self._aws_secret_key = aws_secret_key
        self._aws_region = aws_region
        self._session = aioboto3.Session()
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "loader_initialized",
            bucket=self._bucket,
            prefix=self._prefix or "(root)",
            cache_path=str(self._cache_dir),
        )

    @asynccontextmanager
    async def _get_client(self) -> AsyncGenerator[Any, None]:
        """Yields an active aioboto3 S3 client context."""
        client_context: Any = self._session.client(
            "s3",
            region_name=self._aws_region,
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._aws_access_key,
            aws_secret_access_key=self._aws_secret_key,
        )
        async with client_context as client:
            yield client

    async def resolve(self, model_name: str, version: str) -> str:
        """Downloads artifacts from S3 if missing and returns the local cache path.

        Args:
            model_name: Name of the model.
            version: Version string of the model.

        Returns:
            The local absolute path to the cached artifact directory.

        Raises:
            ArtifactResolutionError: If artifacts cannot be found on S3.
        """
        cached_path = self._cached_path(model_name, version)

        if await asyncio.to_thread(self._is_cache_valid, cached_path):
            logger.debug(
                "cache_hit",
                name=model_name,
                version=version,
                path=str(cached_path),
            )
            return str(cached_path)

        s3_prefix = self._s3_key(model_name, version)

        async with self._get_client() as client:
            objects = await self._list_objects(client, s3_prefix)

            if not objects:
                logger.warning(
                    "model_missing_on_s3_starting_automated_bootstrap",
                    name=model_name,
                    version=version,
                )
                await self._bootstrap_model_to_s3(
                    client, model_name, version, s3_prefix
                )
                objects = await self._list_objects(client, s3_prefix)

                if not objects:
                    raise ArtifactResolutionError(
                        model_name=model_name,
                        version=version,
                        backend=repr(self),
                    )

            await self._download_artifacts(
                client, objects, s3_prefix, cached_path
            )

        logger.info(
            "artifacts_downloaded",
            name=model_name,
            version=version,
            file_count=len(objects),
            path=str(cached_path),
        )
        return str(cached_path)

    async def exists(self, model_name: str, version: str) -> bool:
        """Checks whether artifacts exist under the calculated S3 path.

        Args:
            model_name: Name of the model.
            version: Version string of the model.

        Returns:
            True if matching objects are found, False otherwise.
        """
        s3_prefix = self._s3_key(model_name, version)
        async with self._get_client() as client:
            objects = await self._list_objects(client, s3_prefix)
            return len(objects) > 0

    async def upload(
        self, model_name: str, version: str, local_path: str
    ) -> None:
        """Uploads a local directory tree to the target S3 path.

        Args:
            model_name: Name of the target model.
            version: Target version string.
            local_path: Local source directory containing the files.

        Raises:
            FileNotFoundError: If the source directory does not exist.
            ValueError: If the source directory contains no files.
        """
        source_dir = Path(local_path).resolve()
        if not await asyncio.to_thread(source_dir.is_dir):
            raise FileNotFoundError(
                f"Artifact source directory does not exist: {source_dir}"
            )

        s3_prefix = self._s3_key(model_name, version)

        def _collect_files() -> list[Path]:
            paths: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(source_dir):
                dirnames.sort()
                filenames.sort()
                for filename in filenames:
                    paths.append(Path(dirpath) / filename)
            return paths

        file_paths = await asyncio.to_thread(_collect_files)
        if not file_paths:
            raise ValueError(
                f"Artifact source directory is empty: {source_dir}"
            )

        io_semaphore = asyncio.Semaphore(10)

        async with self._get_client() as client:

            async def _upload_task(file_path: Path) -> None:
                relative_key = file_path.relative_to(source_dir).as_posix()
                s3_key = f"{s3_prefix}{relative_key}"
                async with io_semaphore:
                    await client.upload_file(
                        str(file_path), self._bucket, s3_key
                    )
                logger.debug("file_uploaded", bucket=self._bucket, key=s3_key)

            await asyncio.gather(*(_upload_task(fp) for fp in file_paths))

        logger.info(
            "artifacts_uploaded",
            name=model_name,
            version=version,
            file_count=len(file_paths),
            path=str(source_dir),
        )

    async def invalidate_cache(self, model_name: str, version: str) -> None:
        """Removes the local cached artifact directory.

        Args:
            model_name: Name of the model.
            version: Version string of the model.
        """
        cached_path = self._cached_path(model_name, version)
        if await asyncio.to_thread(cached_path.exists):
            await asyncio.to_thread(shutil.rmtree, cached_path)
            logger.info("cache_invalidated", name=model_name, version=version)

    async def _list_objects(self, client: Any, prefix: str) -> list[str]:
        """Lists object keys matching the given S3 prefix."""
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")

        async for page in paginator.paginate(
            Bucket=self._bucket, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/"):
                    keys.append(key)
        return keys

    async def _download_artifacts(
        self,
        client: Any,
        objects: list[str],
        s3_prefix: str,
        dest: Path,
    ) -> None:
        """Downloads files to a temporary location before swapping to destination."""
        tmp_dir = dest.with_suffix(".tmp")

        if await asyncio.to_thread(tmp_dir.exists):
            await asyncio.to_thread(shutil.rmtree, tmp_dir)
        if await asyncio.to_thread(dest.exists):
            await asyncio.to_thread(shutil.rmtree, dest)

        await asyncio.to_thread(tmp_dir.mkdir, parents=True)
        io_semaphore = asyncio.Semaphore(10)

        async def _download_task(key: str) -> None:
            relative = key[len(s3_prefix) :].lstrip("/")
            local_file = tmp_dir / relative

            await asyncio.to_thread(
                local_file.parent.mkdir, parents=True, exist_ok=True
            )
            async with io_semaphore:
                await client.download_file(self._bucket, key, str(local_file))
            logger.debug("file_downloaded", bucket=self._bucket, key=key)

        try:
            await asyncio.gather(*(_download_task(k) for k in objects))
            await asyncio.to_thread(tmp_dir.rename, dest)
        except Exception:
            if await asyncio.to_thread(tmp_dir.exists):
                await asyncio.to_thread(shutil.rmtree, tmp_dir)
            raise

    async def _bootstrap_model_to_s3(
        self, client: Any, model_name: str, version: str, s3_prefix: str
    ) -> None:
        """Discovers a matching BaseModel class, executes its download hook, and uploads to S3."""
        from galadril_inference.models.base import BaseModel

        def _scan_for_class() -> Any:
            work = list(BaseModel.__subclasses__())
            while work:
                child = work.pop()
                work.extend(child.__subclasses__())
                if not getattr(child, "__abstractmethods__", set()):
                    try:
                        instance = child()
                        meta = instance.meta()
                        if meta.name == model_name and meta.version == version:
                            return child
                    except Exception:
                        continue
            return None

        target_cls = await asyncio.to_thread(_scan_for_class)

        if not target_cls:
            raise ArtifactResolutionError(
                model_name=model_name,
                version=version,
                backend=f"{repr(self)} (Automated bootstrap failed: Model class not found)",
            )

        def _execute_sync_download(download_path: str) -> None:
            model_instance = target_cls()
            if not hasattr(model_instance, "download"):
                raise AttributeError(
                    f"Model class '{target_cls.__name__}' is missing the required 'download' implementation."
                )
            model_instance.download(download_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            logger.info(
                "instantiating_model_for_upstream_download",
                model_name=model_name,
                version=version,
                class_path=f"{target_cls.__module__}.{target_cls.__name__}",
            )

            await asyncio.to_thread(_execute_sync_download, str(tmp_path))

            logger.info(
                "uploading_bootstrapped_artifacts_to_s3",
                model=model_name,
                version=version,
            )

            io_semaphore = asyncio.Semaphore(10)
            local_files = [fp for fp in tmp_path.glob("**/*") if fp.is_file()]

            async def _bootstrap_upload_task(file_path: Path) -> None:
                relative_key = file_path.relative_to(tmp_path)
                s3_key = f"{s3_prefix}{relative_key}"
                async with io_semaphore:
                    await client.upload_file(
                        str(file_path), self._bucket, s3_key
                    )

            await asyncio.gather(
                *(_bootstrap_upload_task(fp) for fp in local_files)
            )

        logger.info(
            "model_bootstrap_completed_and_synced_to_s3",
            model=model_name,
            version=version,
        )

    def _s3_key(self, model_name: str, version: str) -> str:
        """Constructs the canonical S3 destination URI prefix."""
        parts = [self._prefix, model_name, version]
        return "/".join(p for p in parts if p) + "/"

    def _cached_path(self, model_name: str, version: str) -> Path:
        """Generates a uniquely hashed cache subpath for the S3 origin configuration."""
        source_id = hashlib.sha256(
            f"{self._bucket}:{self._prefix}".encode()
        ).hexdigest()[:12]
        return self._cache_dir / source_id / model_name / version

    @staticmethod
    def _is_cache_valid(path: Path) -> bool:
        """Returns True if the path exists and is a non-empty directory."""
        return path.is_dir() and any(path.iterdir())

    def __repr__(self) -> str:
        return (
            f"<S3Loader bucket={self._bucket!r} "
            f"prefix={self._prefix!r} "
            f"cache={str(self._cache_dir)!r}>"
        )
