"""Deterministic inference model used only by the E2E Vision container."""

from __future__ import annotations

from pathlib import Path

from galadril_inference.common.types import (
    ModelMeta,
    PredictionRequest,
    PredictionResult,
)
from galadril_inference.models.base import BaseModel


class E2EDeterministicModel(BaseModel):
    """Produces one fixed embedding without accelerator or network access."""

    __slots__ = ("_loaded",)

    def __init__(self) -> None:
        self._loaded = False

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name="e2e_deterministic",
            version="1.0.0",
            description="Deterministic end-to-end pipeline fixture",
        )

    def load(self, artifact_path: str) -> None:
        marker = Path(artifact_path) / "ready"
        if not marker.is_file():
            raise FileNotFoundError("E2E model marker is missing")
        self._loaded = True

    def download(self, target_path: str) -> None:
        (Path(target_path) / "ready").touch()

    def predict(self, request: PredictionRequest) -> PredictionResult:
        if not self._loaded:
            raise RuntimeError("E2E model is not loaded")
        return PredictionResult(
            model_name=self.meta().name,
            model_version=self.meta().version,
            prediction={
                "embedding": [0.25, 0.5, 0.75, 1.0],
                "label": "gateway-e2e-record",
                "confidence": 0.99,
            },
            confidence=0.99,
            request_id=request.request_id,
        )

    def input_schema(self) -> dict[str, object]:
        return {"type": "object"}

    def output_schema(self) -> dict[str, object]:
        return {"type": "object"}

    def cleanup(self) -> None:
        self._loaded = False
