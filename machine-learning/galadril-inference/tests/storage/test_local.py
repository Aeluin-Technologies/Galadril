from pathlib import Path
import pytest

from galadril_inference.common.exceptions import ArtifactResolutionError
from galadril_inference.storage.local import LocalLoader


def test_init_success(tmp_path: Path) -> None:
    """Test successful initialization of LocalLoader.

    Args:
        tmp_path: Pytest fixture providing a temporary directory path.
    """
    loader = LocalLoader(base_path=tmp_path)
    assert loader.base_path == tmp_path.resolve()


def test_init_raises_file_not_found_error(tmp_path: Path) -> None:
    """Test initialization fails when base_path does not exist.

    Args:
        tmp_path: Pytest fixture providing a temporary directory path.
    """
    missing_path = tmp_path / "missing_dir"
    with pytest.raises(FileNotFoundError):
        LocalLoader(base_path=missing_path)


def test_resolve_success(tmp_path: Path) -> None:
    """Test resolving an existing and non-empty artifact directory.

    Args:
        tmp_path: Pytest fixture providing a temporary directory path.
    """
    model_name = "test_model"
    version = "v1"
    artifact_dir = tmp_path / model_name / version
    artifact_dir.mkdir(parents=True)

    (artifact_dir / "model.bin").touch()

    loader = LocalLoader(base_path=tmp_path)
    resolved_path = loader.resolve(model_name=model_name, version=version)

    assert resolved_path == str(artifact_dir.resolve())


def test_resolve_raises_when_missing(tmp_path: Path) -> None:
    """Test resolving raises an error when the artifact directory is missing.

    Args:
        tmp_path: Pytest fixture providing a temporary directory path.
    """
    loader = LocalLoader(base_path=tmp_path)

    with pytest.raises(ArtifactResolutionError):
        loader.resolve(model_name="missing_model", version="v1")


def test_resolve_raises_when_empty(tmp_path: Path) -> None:
    """Test resolving raises an error when the artifact directory is empty.

    Args:
        tmp_path: Pytest fixture providing a temporary directory path.
    """
    model_name = "test_model"
    version = "v1"
    artifact_dir = tmp_path / model_name / version
    artifact_dir.mkdir(parents=True)
    # The directory is created but left empty.

    loader = LocalLoader(base_path=tmp_path)

    with pytest.raises(ArtifactResolutionError):
        loader.resolve(model_name=model_name, version=version)


def test_exists(tmp_path: Path) -> None:
    """Test existence checking for model versions.

    Args:
        tmp_path: Pytest fixture providing a temporary directory path.
    """
    loader = LocalLoader(base_path=tmp_path)
    model_name = "test_model"

    assert not loader.exists(model_name=model_name, version="v1")

    artifact_dir = tmp_path / model_name / "v1"
    artifact_dir.mkdir(parents=True)

    # Directory is empty, so exists() should return False
    assert not loader.exists(model_name=model_name, version="v1")

    # Add a file to make it non-empty
    (artifact_dir / "model.bin").touch()
    assert loader.exists(model_name=model_name, version="v1")


def test_list_versions(tmp_path: Path) -> None:
    """Test listing available versions for a model.

    Args:
        tmp_path: Pytest fixture providing a temporary directory path.
    """
    loader = LocalLoader(base_path=tmp_path)
    model_name = "test_model"

    assert loader.list_versions(model_name=model_name) == []

    for ver in ["v2", "v1", "v3"]:
        ver_dir = tmp_path / model_name / ver
        ver_dir.mkdir(parents=True)
        (ver_dir / "model.bin").touch()

    (tmp_path / model_name / "empty_v4").mkdir()

    versions = loader.list_versions(model_name=model_name)
    assert versions == ["v1", "v2", "v3"]
