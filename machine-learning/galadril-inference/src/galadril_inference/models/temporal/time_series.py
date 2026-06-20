"""Time series forecasting model powered by Google's TimesFM."""

from __future__ import annotations
from typing import Any
from pathlib import Path

import numpy as np
import structlog

from galadril_inference.common.exceptions import (
    ModelLoadError,
    SchemaValidationError,
)
from galadril_inference.common.types import (
    ModelMeta,
    PredictionRequest,
    PredictionResult,
)
from galadril_inference.models.base import BaseModel

logger = structlog.get_logger(__name__)

_MODEL_NAME = "timesfm_forecast"
_MODEL_VERSION = "2.5.0"


class TimesFMModel(BaseModel):
    """Google TimesFM 2.5 200m model for time series forecasting."""

    def __init__(self) -> None:
        self._session: Any | None = None

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Zero-shot time series forecasting using google/timesfm-2.5-200m.",
            tags={
                "domain": "time_series",
                "backend": "onnxruntime",
                "framework": "onnx",
            },
        )

    def load(self, artifact_path: str) -> None:
        """Load the TimesFM model."""
        import os
        import glob

        try:
            import onnxruntime as ort
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "onnxruntime or huggingface_hub is not installed. Install with: uv add onnxruntime huggingface_hub",
            ) from exc

        try:
            model_dir = artifact_path if artifact_path else "onnx"
            os.makedirs(model_dir, exist_ok=True)
            onnx_files = glob.glob(
                os.path.join(model_dir, "**", "*.onnx"), recursive=True
            )

            if not onnx_files:
                snapshot_download(
                    repo_id="pdufour/timesfm-2.5-200m-transformers-onnx",
                    local_dir=model_dir,
                    allow_patterns=["*.onnx"],
                )
                onnx_files = glob.glob(
                    os.path.join(model_dir, "**", "*.onnx"), recursive=True
                )

            if not onnx_files:
                raise FileNotFoundError(
                    f"No .onnx file could be found or downloaded in {model_dir}"
                )

            model_path = onnx_files[0]

            self._session = ort.InferenceSession(
                model_path,
                providers=[
                    "CUDAExecutionProvider",
                    # "CoreMLExecutionProvider", it freezes my machine.
                    "CPUExecutionProvider",
                ],
            )

            logger.info("model_loaded", model_name=_MODEL_NAME, path=model_path)
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str) -> None:
        """Download the TimesFM ONNX export into target_path."""
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "huggingface_hub is not installed.",
            ) from exc

        try:
            Path(target_path).mkdir(parents=True, exist_ok=True)
            snapshot_download(
                repo_id="pdufour/timesfm-2.5-200m-transformers-onnx",
                local_dir=target_path,
                allow_patterns=["*.onnx"],
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release the model from memory."""
        self._session = None
        logger.info("model_cleaned_up", model_name=_MODEL_NAME)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run forecasting inference."""
        self._ensure_loaded()

        history = self._extract_history(request)
        horizon = self._extract_horizon(request)

        try:
            inp = self._session.get_inputs()[0]
            inp_name = inp.name
            expected_shape = inp.shape
            past_values = np.asarray([history], dtype=np.float32)
            expected_context = expected_shape[1]

            if isinstance(expected_context, int):
                current_len = past_values.shape[1]
                if current_len < expected_context:
                    pad_width = expected_context - current_len
                    past_values = np.pad(
                        past_values, ((0, 0), (pad_width, 0)), mode="edge"
                    )
                elif current_len > expected_context:
                    past_values = past_values[:, -expected_context:]

            expected_batch = expected_shape[0]
            if isinstance(expected_batch, int) and expected_batch > 1:
                past_values = np.repeat(past_values, expected_batch, axis=0)

            _, mean_predictions, full_predictions = self._session.run(
                None, {inp_name: past_values}
            )

            mean_pred = mean_predictions[0]
            full_pred = full_predictions[0]
            horizon_out = min(horizon, mean_pred.shape[0])

            prediction_payload: dict[str, Any] = {
                "horizon": horizon_out,
                "point_forecast": mean_pred[:horizon_out].tolist(),
                "quantiles": full_pred[:horizon_out].tolist(),
            }

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction=prediction_payload,
                confidence=1.0,
            )

        except SchemaValidationError:
            raise
        except Exception as exc:
            raise RuntimeError(f"TimesFM ONNX inference failed: {exc}") from exc

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["history"],
            "properties": {
                "history": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Historical time series values.",
                },
                "horizon": {
                    "type": "integer",
                    "description": "Number of steps to forecast.",
                    "default": 24,
                },
            },
        }

    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "horizon": {"type": "integer"},
                "point_forecast": {
                    "type": "array",
                    "items": {"type": "number"},
                },
                "quantiles": {
                    "type": "array",
                    "description": "Raw quantile outputs. Shape: [horizon, num_quantiles].",
                    "items": {"type": "array", "items": {"type": "number"}},
                },
            },
        }

    def _ensure_loaded(self) -> None:
        if self._session is None:
            raise ModelLoadError(
                _MODEL_NAME,
                "Model is not loaded. Call load() before predict().",
            )

    @staticmethod
    def _extract_horizon(request: PredictionRequest) -> int:
        horizon = request.features.get("horizon", 24)
        if not isinstance(horizon, int):
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Feature 'horizon' must be an int, got {type(horizon).__name__}."
                ],
            )
        if horizon <= 0:
            raise SchemaValidationError(
                _MODEL_NAME, ["Feature 'horizon' must be > 0."]
            )
        return horizon

    @staticmethod
    def _extract_history(request: PredictionRequest) -> list[float]:
        history = request.features.get("history")
        if history is None:
            raise SchemaValidationError(
                _MODEL_NAME, ["Missing required feature: 'history'."]
            )
        if not isinstance(history, list):
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Feature 'history' must be a list, got {type(history).__name__}."
                ],
            )
        if not history:
            raise SchemaValidationError(
                _MODEL_NAME, ["Feature 'history' cannot be empty."]
            )
        return [float(x) for x in history]
