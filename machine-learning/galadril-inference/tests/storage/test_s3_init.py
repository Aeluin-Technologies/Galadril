"""Regression tests for S3 artifact loader initialization."""

from pathlib import Path

import pytest
from galadril_inference.storage.s3 import S3Loader


def test_s3_loader_initializes_storage_coordinates(tmp_path: Path) -> None:
    """Test constructor retains canonical bucket and prefix coordinates."""
    loader = S3Loader(
        bucket="models",
        prefix="/tenant-a/models/",
        cache_dir=tmp_path,
    )

    assert "bucket='models'" in repr(loader)
    assert "prefix='tenant-a/models'" in repr(loader)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
