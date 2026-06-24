"""Local filesystem artifact loader."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import structlog

from galadril_inference.common.exceptions import ArtifactResolutionError
from galadril_inference.loading.loader import ArtifactLoader

logger = structlog.get_logger(__name__)


class LocalLoader(ArtifactLoader):
    """Loads model artifacts from the local filesystem."""

    def __init__(self, base_path: str | Path) -> None:
        """Initializes the loader.

        Args:
            base_path: Root directory containing all model artifacts.

        Raises:
            FileNotFoundError: If the base path does not exist or is not a directory.
        """
        self._base_path = Path(base_path).resolve()

        if not self._base_path.is_dir():
            raise FileNotFoundError(
                f"Artifact base path does not exist: {self._base_path}",
            )

        logger.info("loader_initialized", base_path=str(self._base_path))

    @property
    def base_path(self) -> Path:
        """Returns the base path directory."""
        return self._base_path

    async def resolve(self, model_name: str, version: str) -> str:
        """Returns the local path to the model's versioned artifact directory.

        Args:
            model_name: Name of the model.
            version: Version string of the model.

        Returns:
            The absolute path to the artifact directory.

        Raises:
            ArtifactResolutionError: If the directory does not exist or is empty.
        """
        artifact_dir = self._base_path / model_name / version

        def _verify_and_check() -> tuple[bool, bool]:
            is_dir = artifact_dir.is_dir()
            return is_dir, is_dir and any(artifact_dir.iterdir())

        is_dir, exists_and_populated = await asyncio.to_thread(
            _verify_and_check
        )

        if not is_dir:
            raise ArtifactResolutionError(
                model_name=model_name,
                version=version,
                backend=repr(self),
            )

        if not exists_and_populated:
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

    async def exists(self, model_name: str, version: str) -> bool:
        """Checks whether a non-empty artifact directory exists on disk.

        Args:
            model_name: Name of the model.
            version: Version string of the model.

        Returns:
            True if the directory exists and contains files, False otherwise.
        """
        artifact_dir = self._base_path / model_name / version

        def _check() -> bool:
            return artifact_dir.is_dir() and any(artifact_dir.iterdir())

        return await asyncio.to_thread(_check)

    async def upload(
        self, model_name: str, version: str, local_path: str
    ) -> None:
        """Copies a local artifact directory into the loader storage tree.

        Args:
            model_name: Name of the target model.
            version: Target version string.
            local_path: Source directory path on the local filesystem.

        Raises:
            FileNotFoundError: If the source directory does not exist.
            ValueError: If the source directory contains no files.
        """
        source_dir = Path(local_path).resolve()
        if not await asyncio.to_thread(source_dir.is_dir):
            raise FileNotFoundError(
                f"Artifact source directory does not exist: {source_dir}"
            )

        target_dir = self._base_path / model_name / version

        def _sync_tree_copy() -> int:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)

            counter = 0
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
                    counter += 1
            return counter

        uploaded_count = await asyncio.to_thread(_sync_tree_copy)

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

    async def list_versions(self, model_name: str) -> list[str]:
        """Returns all available versions for a given model, sorted ascending.

        Args:
            model_name: Name of the model.

        Returns:
            A sorted list of version strings.
        """
        model_dir = self._base_path / model_name
        if not await asyncio.to_thread(model_dir.is_dir):
            return []

        def _scan_versions() -> list[str]:
            return sorted(
                d.name
                for d in model_dir.iterdir()
                if d.is_dir() and any(d.iterdir())
            )

        return await asyncio.to_thread(_scan_versions)

    def __repr__(self) -> str:
        return f"<LocalLoader base_path={str(self._base_path)!r}>"
