import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import aioboto3
import pytest
from galadril_inference.common.exceptions import ArtifactResolutionError
from galadril_inference.storage.s3 import S3Loader
from moto import mock_aws


@pytest.fixture
def aws_credentials() -> None:
    """Mock AWS credentials for testing."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
async def mock_s3_env(
    aws_credentials: None,
) -> AsyncGenerator[tuple[Any, str], None]:
    """Provide a mocked S3 environment prepopulated with dummy data.

    Yields:
        A tuple containing the active aioboto3 client and the mock bucket name.
    """
    with mock_aws():
        session = aioboto3.Session()
        async with session.client("s3", region_name="us-east-1") as client:  # type: ignore
            bucket_name = "models"
            await client.create_bucket(Bucket=bucket_name)

            await client.put_object(
                Bucket=bucket_name,
                Key="test_model/v1/config.json",
                Body=b'{"type": "mock"}',
            )
            await client.put_object(
                Bucket=bucket_name,
                Key="test_model/v1/weights.bin",
                Body=b"01010101",
            )

            yield client, bucket_name


@pytest.mark.asyncio
async def test_s3_loader_upload_and_resolve_nested_artifacts(
    mock_s3_env: tuple[Any, str],
    tmp_path: Path,
) -> None:
    """Test uploading a nested artifact tree and resolving it back locally."""
    s3_client, bucket_name = mock_s3_env
    cache_dir = tmp_path / "cache"
    source_dir = tmp_path / "source"
    nested_dir = source_dir / "assets" / "nested"
    nested_dir.mkdir(parents=True)
    (source_dir / "config.json").write_text('{"type": "uploaded"}')
    (nested_dir / "weights.bin").write_bytes(b"01010101")

    loader = S3Loader(
        bucket=bucket_name,
        prefix="",
        cache_dir=cache_dir,
    )

    await loader.upload("uploaded_model", "v1", str(source_dir))

    assert await loader.exists("uploaded_model", "v1") is True

    config_response = await s3_client.get_object(
        Bucket=bucket_name,
        Key="uploaded_model/v1/config.json",
    )
    async with config_response["Body"] as stream:
        assert (await stream.read()).decode() == '{"type": "uploaded"}'

    weights_response = await s3_client.get_object(
        Bucket=bucket_name,
        Key="uploaded_model/v1/assets/nested/weights.bin",
    )
    async with weights_response["Body"] as stream:
        assert await stream.read() == b"01010101"

    resolved_path = Path(await loader.resolve("uploaded_model", "v1"))
    assert resolved_path.is_dir()
    assert (resolved_path / "config.json").read_text() == '{"type": "uploaded"}'
    assert (
        resolved_path / "assets" / "nested" / "weights.bin"
    ).read_bytes() == b"01010101"


@pytest.mark.asyncio
async def test_s3_loader_resolve_success(
    mock_s3_env: tuple[Any, str],
    tmp_path: Path,
) -> None:
    """Test resolving downloads files from S3 and caches them locally."""
    _, bucket_name = mock_s3_env
    loader = S3Loader(
        bucket=bucket_name,
        prefix="",
        cache_dir=tmp_path,
    )

    resolved_path_str = await loader.resolve("test_model", "v1")
    resolved_path = Path(resolved_path_str)

    assert resolved_path.is_dir()
    assert (resolved_path / "config.json").exists()
    assert (resolved_path / "weights.bin").exists()
    assert (resolved_path / "config.json").read_text() == '{"type": "mock"}'


@pytest.mark.asyncio
async def test_s3_loader_resolve_cache_hit(
    mock_s3_env: tuple[Any, str],
    tmp_path: Path,
) -> None:
    """Test resolving uses the local cache if it is already valid."""
    s3_client, bucket_name = mock_s3_env
    loader = S3Loader(
        bucket=bucket_name,
        prefix="",
        cache_dir=tmp_path,
    )

    path1 = await loader.resolve("test_model", "v1")

    await s3_client.delete_object(
        Bucket=bucket_name, Key="test_model/v1/config.json"
    )
    await s3_client.delete_object(
        Bucket=bucket_name, Key="test_model/v1/weights.bin"
    )

    path2 = await loader.resolve("test_model", "v1")

    assert path1 == path2


@pytest.mark.asyncio
async def test_s3_loader_resolve_raises_when_missing(
    mock_s3_env: tuple[Any, str],
    tmp_path: Path,
) -> None:
    """Test resolving an unknown model raises an ArtifactResolutionError."""
    _, bucket_name = mock_s3_env
    loader = S3Loader(
        bucket=bucket_name,
        prefix="",
        cache_dir=tmp_path,
    )

    with pytest.raises(ArtifactResolutionError):
        await loader.resolve("missing_model", "v1")


@pytest.mark.asyncio
async def test_s3_loader_exists(
    mock_s3_env: tuple[Any, str],
    tmp_path: Path,
) -> None:
    """Test existence checking directly queries S3 correctly."""
    _, bucket_name = mock_s3_env
    loader = S3Loader(
        bucket=bucket_name,
        prefix="",
        cache_dir=tmp_path,
    )

    assert await loader.exists("test_model", "v1") is True
    assert await loader.exists("test_model", "v2") is False


@pytest.mark.asyncio
async def test_s3_loader_invalidate_cache(
    mock_s3_env: tuple[Any, str],
    tmp_path: Path,
) -> None:
    """Test cache invalidation correctly removes the local directory."""
    _, bucket_name = mock_s3_env
    loader = S3Loader(
        bucket=bucket_name,
        prefix="",
        cache_dir=tmp_path,
    )

    resolved_path = Path(await loader.resolve("test_model", "v1"))
    assert resolved_path.exists()

    await loader.invalidate_cache("test_model", "v1")
    assert not resolved_path.exists()
