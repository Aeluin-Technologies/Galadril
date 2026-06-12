"""Local filesystem artifact loader."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import structlog

from galadril_inference.common.exceptions import ArtifactResolutionError
from galadril_inference.loading.loader import ArtifactLoader

logger = structlog.get_logger(__name__)


class LocalLoader(ArtifactLoader):
    """Load model artifacts from the local filesystem.

    Args:

        base_path: Root directory containing all model artifacts.
                   Each model lives under ``<base_path>/<name>/<version>/``.
    """

    def __init__(self, base_path: str | Path) -> None:
        self._base_path = Path(base_path).resolve()

        if not self._base_path.is_dir():
            raise FileNotFoundError(
                f"Artifact base path does not exist: {self._base_path}",
            )

        logger.info("loader_initialized", base_path=str(self._base_path))

    @property
    def base_path(self) -> Path:
        return self._base_path

    def resolve(self, model_name: str, version: str) -> str:
        """Return the local path to the model's versioned artifact directory.

        Raises:

            ArtifactResolutionError: if the directory does not exist or is
            empty.
        """
        artifact_dir = self._base_path / model_name / version

        if not artifact_dir.is_dir():
            raise ArtifactResolutionError(
                model_name=model_name,
                version=version,
                backend=repr(self),
            )

        if not any(artifact_dir.iterdir()):
            raise ArtifactResolutionError(
                model_name=model_name,
                version=version,
                backend=f"{self!r} (directory exists but is empty)",
            )

        path = str(artifact_dir)
        logger.debug(
            "artifact_resolved",
            name=model_name,
            version=version,
            path=path,
        )
        return path

    def exists(self, model_name: str, version: str) -> bool:
        """Check whether a non-empty artifact directory exists."""
        artifact_dir = self._base_path / model_name / version
        return artifact_dir.is_dir() and any(artifact_dir.iterdir())

    def upload(self, model_name: str, version: str, local_path: str) -> None:
        """Copy a local artifact directory into the loader storage tree."""
        source_dir = Path(local_path).resolve()
        if not source_dir.is_dir():
            raise FileNotFoundError(
                f"Artifact source directory does not exist: {source_dir}"
            )

        target_dir = self._base_path / model_name / version
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        uploaded_count = 0
        for dirpath, dirnames, filenames in os.walk(source_dir):
            dirnames.sort()
            filenames.sort()
            current_dir = Path(dirpath)

            for filename in filenames:
                source_file = current_dir / filename
                relative_path = source_file.relative_to(source_dir)
                destination_file = target_dir / relative_path
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination_file)
                uploaded_count += 1

        if uploaded_count == 0:
            raise ValueError(
                f"Artifact source directory is empty: {source_dir}"
            )

        logger.info(
            "artifacts_uploaded",
            name=model_name,
            version=version,
            file_count=uploaded_count,
            path=str(source_dir),
        )

    def list_versions(self, model_name: str) -> list[str]:
        """Return all available versions for a given model, sorted ascending."""
        model_dir = self._base_path / model_name
        if not model_dir.is_dir():
            return []
        return sorted(
            d.name
            for d in model_dir.iterdir()
            if d.is_dir() and any(d.iterdir())
        )

    def __repr__(self) -> str:
        return f"<LocalLoader base_path={str(self._base_path)!r}>"
