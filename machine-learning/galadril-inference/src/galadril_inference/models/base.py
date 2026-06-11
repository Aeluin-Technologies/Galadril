"""Contract for pluggable inference models.

To register a new model, create a file in galadril_inference/models/,
subclass BaseModel, and implement all abstract methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from galadril_inference.common.types import (
    ModelMeta,
    PredictionRequest,
    PredictionResult,
)


class BaseModel(ABC):
    """Contract for a pluggable inference model."""

    @abstractmethod
    def meta(self) -> ModelMeta:
        """Return the immutable identity of this model."""

    @abstractmethod
    def load(self, artifact_path: str) -> None:
        """Load model weights and artifacts from the resolved path.

        Must be idempotent: calling load() twice should not corrupt state.
        Raise ModelLoadError on failure.
        """

    @abstractmethod
    def download(self, target_path: str) -> None:
        """Download the raw model weights/artifacts from an upstream source into target_path."""

    @abstractmethod
    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run inference on a single request. This is the hot path.

        The implementation MUST return a PredictionResult with at least
        model_name, model_version, and prediction filled in.
        latency_ms and request_id are handled by the engine.
        """

    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """Return a JSON Schema dict describing expected input features.

        Used by the validation layer to reject malformed requests early.
        """

    @abstractmethod
    def output_schema(self) -> dict[str, Any]:
        """Return a JSON Schema dict describing the prediction output.

        Used for downstream contract enforcement.
        """

    @abstractmethod
    def cleanup(self) -> None:
        """Release held resources (GPU memory, file handles, caches).

        Optional override. Called during engine shutdown.
        Default implementation is a no-op.
        """

    def __repr__(self) -> str:
        m = self.meta()
        return f"<{type(self).__name__} name={m.name!r} version={m.version!r}>"
