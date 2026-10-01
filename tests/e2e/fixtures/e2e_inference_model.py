"""Deterministic inference model used only by the E2E Vision container."""

from __future__ import annotations

import hashlib
import json
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


class _RetailRecord(PayloadModel):
    """Accepts only stable source identity evidence needed for mock inference."""

    model_config = ConfigDict(extra="allow", strict=True)

    customer_id: int = Field(gt=0)
    source_row: int = Field(gt=0)


class _TrialRecord(PayloadModel):
    """Bounds an anonymous randomized-trial row without participant attributes."""

    model_config = ConfigDict(extra="forbid", strict=True)

    record_id: int = Field(gt=0)
    condition: Literal[1, 6]
    units_selected: float = Field(ge=0.0, allow_inf_nan=False)


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
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("E2E evidence must be an object")
            if "customer_id" in parsed:
                retail = _RetailRecord.model_validate(parsed)
                digest = hashlib.shake_256(
                    str(retail.customer_id).encode("ascii")
                ).digest(1024)
                prediction = {
                    "embedding": [
                        (1.0 if byte & 1 else -1.0) / 32.0 for byte in digest
                    ],
                    "label": "retail-customer",
                    "source_field": str(retail.customer_id),
                    "scalar_evidence": {"source_row": retail.source_row},
                    "confidence": 0.99,
                }
            elif "record_id" in parsed:
                trial = _TrialRecord.model_validate(parsed)
                digest = hashlib.shake_256(
                    f"trial:{trial.record_id}".encode("ascii")
                ).digest(1024)
                prediction = {
                    "embedding": [
                        (1.0 if byte & 1 else -1.0) / 32.0 for byte in digest
                    ],
                    "label": "trial-participant",
                    "source_field": f"trial:{trial.record_id}",
                    "scalar_evidence": {
                        "treatment": 1.0 if trial.condition == 1 else 0.0,
                        "outcome": trial.units_selected,
                    },
                    "confidence": 0.99,
                }
            else:
                record = _CausalRecord.model_validate(parsed)
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
