import os
from pathlib import Path
from typing import Generator

import boto3
import pytest
from moto import mock_aws

from galadril_inference.common.exceptions import ArtifactResolutionError
from galadril_inference.storage.s3 import S3Loader


@pytest.fixture
def aws_credentials() -> None:
    """Mock AWS credentials for testing."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def s3_client(aws_credentials: None) -> Generator[boto3.client, None, None]:
    """Provide a mocked S3 client.

    Yields:
        A mocked boto3 S3 client inside the `mock_aws` context.
    """
    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


@pytest.fixture
def setup_bucket(s3_client: boto3.client) -> str:
    """Create a mock bucket and populate it with dummy data.

    Args:
        s3_client: The mocked boto3 S3 client.

    Returns:
        The name of the created S3 bucket.
    """
    bucket_name = "test-models-bucket"
    s3_client.create_bucket(Bucket=bucket_name)
    s3_client.put_object(
        Bucket=bucket_name,
        Key="models/test_model/v1/config.json",
        Body=b'{"type": "mock"}',
    )
    s3_client.put_object(
        Bucket=bucket_name,
        Key="models/test_model/v1/weights.bin",
        Body=b"01010101",
    )
    return bucket_name


def test_s3_loader_resolve_success(
    s3_client: boto3.client,
    setup_bucket: str,
    tmp_path: Path,
) -> None:
    """Test resolving downloads files from S3 and caches them locally.

    Args:
        s3_client: The mocked S3 client.
        setup_bucket: The name of the initialized mock bucket.
        tmp_path: Pytest fixture providing a temporary cache directory.
    """
    loader = S3Loader(
        bucket=setup_bucket,
        prefix="models",
        s3_client=s3_client,
        cache_dir=tmp_path,
    )

    resolved_path_str = loader.resolve("test_model", "v1")
    resolved_path = Path(resolved_path_str)

    assert resolved_path.is_dir()
    assert (resolved_path / "config.json").exists()
    assert (resolved_path / "weights.bin").exists()
    assert (resolved_path / "config.json").read_text() == '{"type": "mock"}'


def test_s3_loader_resolve_cache_hit(
    s3_client: boto3.client,
    setup_bucket: str,
    tmp_path: Path,
) -> None:
    """Test resolving uses the local cache if it is already valid.

    Args:
        s3_client: The mocked S3 client.
        setup_bucket: The name of the initialized mock bucket.
        tmp_path: Pytest fixture providing a temporary cache directory.
    """
    loader = S3Loader(
        bucket=setup_bucket,
        prefix="models",
        s3_client=s3_client,
        cache_dir=tmp_path,
    )

    path1 = loader.resolve("test_model", "v1")

    s3_client.delete_object(
        Bucket=setup_bucket, Key="models/test_model/v1/config.json"
    )
    s3_client.delete_object(
        Bucket=setup_bucket, Key="models/test_model/v1/weights.bin"
    )

    path2 = loader.resolve("test_model", "v1")

    assert path1 == path2


def test_s3_loader_resolve_raises_when_missing(
    s3_client: boto3.client,
    setup_bucket: str,
    tmp_path: Path,
) -> None:
    """Test resolving an unknown model raises an ArtifactResolutionError.

    Args:
        s3_client: The mocked S3 client.
        setup_bucket: The name of the initialized mock bucket.
        tmp_path: Pytest fixture providing a temporary cache directory.
    """
    loader = S3Loader(
        bucket=setup_bucket,
        prefix="models",
        s3_client=s3_client,
        cache_dir=tmp_path,
    )

    with pytest.raises(ArtifactResolutionError):
        loader.resolve("missing_model", "v1")


def test_s3_loader_exists(
    s3_client: boto3.client,
    setup_bucket: str,
    tmp_path: Path,
) -> None:
    """Test existence checking directly queries S3 correctly.

    Args:
        s3_client: The mocked S3 client.
        setup_bucket: The name of the initialized mock bucket.
        tmp_path: Pytest fixture providing a temporary cache directory.
    """
    loader = S3Loader(
        bucket=setup_bucket,
        prefix="models",
        s3_client=s3_client,
        cache_dir=tmp_path,
    )

    assert loader.exists("test_model", "v1") is True
    assert loader.exists("test_model", "v2") is False


def test_s3_loader_invalidate_cache(
    s3_client: boto3.client,
    setup_bucket: str,
    tmp_path: Path,
) -> None:
    """Test cache invalidation correctly removes the local directory.

    Args:
        s3_client: The mocked S3 client.
        setup_bucket: The name of the initialized mock bucket.
        tmp_path: Pytest fixture providing a temporary cache directory.
    """
    loader = S3Loader(
        bucket=setup_bucket,
        prefix="models",
        s3_client=s3_client,
        cache_dir=tmp_path,
    )

    resolved_path = Path(loader.resolve("test_model", "v1"))
    assert resolved_path.exists()

    loader.invalidate_cache("test_model", "v1")
    assert not resolved_path.exists()
