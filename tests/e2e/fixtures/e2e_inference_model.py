"""Deterministic inference model used only by the E2E Vision container."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from galadril_inference.common.types import (
    ModelMeta,
    PredictionRequest,
    PredictionResult,
)
from galadril_inference.models.base import BaseModel
from pydantic import BaseModel as PayloadModel
from pydantic import ConfigDict, Field


class _CausalRecord(PayloadModel):
    """Bounds the deterministic evidence accepted by the E2E model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    cohort: Literal["alpha", "decoy"]
    signal: float = Field(ge=0.0, le=1.0)
    outcome: float = Field(ge=0.0, le=1.0)
    decoy: bool


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
        content = request.features.get("data")
        prediction: dict[str, object] = {
            "embedding": [0.25, 0.5, 0.75, 1.0],
            "label": "gateway-e2e-record",
            "confidence": 0.99,
        }
        confidence = 0.99
        if isinstance(content, str) and content.startswith("{"):
            record = _CausalRecord.model_validate_json(content)
            if record.decoy != (record.cohort == "decoy"):
                raise ValueError(
                    "Causal fixture decoy flag does not match cohort"
                )
            prediction = {
                "embedding": (
                    [1.0, record.signal * 0.05, 0.0, 0.0]
                    if record.cohort == "alpha"
                    else [0.0, 0.0, 1.0, record.signal * 0.05]
                ),
                "label": "gateway-e2e-record",
                "source_field": record.cohort,
                "confidence": record.outcome,
            }
            confidence = record.outcome
        return PredictionResult(
            model_name=self.meta().name,
            model_version=self.meta().version,
            prediction=prediction,
            confidence=confidence,
            request_id=request.request_id,
        )

    def input_schema(self) -> dict[str, object]:
        return {"type": "object"}

    def output_schema(self) -> dict[str, object]:
        return {"type": "object"}

    def cleanup(self) -> None:
        self._loaded = False
