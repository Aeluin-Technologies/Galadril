"""Abstract interface for artifact loading backends."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ArtifactLoader(ABC):
    """Resolves and provides access to model artifact paths."""

    @abstractmethod
    def resolve(self, model_name: str, version: str) -> str:
        """Return the local filesystem path to the model artifacts.

        If the artifacts are remote (S3, MLflow), this method must
        download them first and return a local cache path.

        Args:

            model_name: The unique model identifier (e.g. "face_recognition").
            version: The semantic version string (e.g. "1.0.0").

        Returns:

            Absolute path to a directory containing model artifacts.

        Raises:

            ArtifactResolutionError: if the artifact cannot be found.
        """
        ...

    @abstractmethod
    def exists(self, model_name: str, version: str) -> bool:
        """Check whether artifacts exist for this model + version."""
        ...

    @abstractmethod
    def upload(self, model_name: str, version: str, local_path: str) -> None:
        """Upload locally downloaded model artifacts to the remote storage backend (e.g. S3).

        Args:

            model_name: The unique model identifier.
            version: The semantic version string.
            local_path: Absolute path to the local directory containing the artifacts.
        """
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"
