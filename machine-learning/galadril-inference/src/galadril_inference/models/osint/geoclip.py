from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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

_MODEL_NAME = "geoclip"
_MODEL_VERSION = "1.0.0"


class GeoCLIPModel(BaseModel):
    """Pluggable GeoCLIP model supporting image geo-localization and worldwide GPS embeddings."""

    def __init__(self) -> None:
        self._model: Any | None = None
        self._gps_encoder: Any | None = None
        self._device: str = "cpu"

    def meta(self) -> ModelMeta:
        """Return the immutable identity of this model."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description=(
                "GeoCLIP: Clip-Inspired Alignment between Locations and Images "
                "for Effective Worldwide Geo-localization and GPS Embeddings."
            ),
            tags={
                "domain": "multimodal",
                "backend": "pytorch",
                "framework": "geoclip",
                "task": "geo-localization",
            },
        )

    def load(
        self,
        artifact_path: str,
        device: str = "cpu",
    ) -> None:
        """Load GeoCLIP components.

        Args:
            artifact_path: Local directory path (not strictly used by geoclip's
                           internal weights loader but required by contract).
            device: Computing device ('cpu', 'cuda', 'mps').
        """
        try:
            import torch
            from geoclip import GeoCLIP, LocationEncoder
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "torch or geoclip is not installed. Please run `pip install geoclip torch`.",
            ) from exc

        if self._model is not None and self._device == device:
            return

        self._device = device
        os.makedirs(artifact_path, exist_ok=True)

        try:
            logger.info(
                "Loading GeoCLIP model and GPS encoder...", device=device
            )

            self._model = GeoCLIP()
            self._model.to(self._device)
            self._model.eval()

            self._gps_encoder = LocationEncoder()
            self._gps_encoder.to(self._device)
            self._gps_encoder.eval()

        except Exception as exc:
            self.cleanup()
            raise ModelLoadError(
                _MODEL_NAME, f"Failed to instantiate GeoCLIP: {exc}"
            ) from exc

    def download(self, target_path: str) -> None:
        """Create a small artifact manifest for GeoCLIP bootstrap flows."""
        try:
            root = Path(target_path)
            root.mkdir(parents=True, exist_ok=True)
            manifest_path = root / "artifact_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "model_name": _MODEL_NAME,
                        "version": _MODEL_VERSION,
                        "note": (
                            "GeoCLIP resolves its pretrained weights at runtime "
                            "through the installed package."
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release held PyTorch tensors and clear models from memory."""
        if self._model is not None:
            del self._model
            self._model = None
        if self._gps_encoder is not None:
            del self._gps_encoder
            self._gps_encoder = None

        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run GeoCLIP inference. Supports both Image Geo-localization and GPS Encoding."""
        self._ensure_loaded()

        import torch

        features = request.features
        task = features.get("task", "image_to_gps")

        if task == "image_to_gps":
            image_path = features.get("image_path")
            if not image_path:
                raise SchemaValidationError(
                    _MODEL_NAME,
                    ["'image_path' is required for 'image_to_gps' task."],
                )

            if not os.path.exists(image_path):
                raise FileNotFoundError(
                    f"Image not found at path: {image_path}"
                )

            top_k = features.get("top_k", 5)

            try:
                top_pred_gps, top_pred_prob = self._model.predict(
                    image_path, top_k=top_k
                )

                predictions = []
                for i in range(len(top_pred_gps)):
                    predictions.append(
                        {
                            "latitude": float(top_pred_gps[i][0]),
                            "longitude": float(top_pred_gps[i][1]),
                            "probability": float(top_pred_prob[i]),
                        }
                    )

                return PredictionResult(
                    model_name=_MODEL_NAME,
                    model_version=_MODEL_VERSION,
                    prediction={
                        "task": "image_to_gps",
                        "predictions": predictions,
                    },
                    confidence=float(top_pred_prob[0])
                    if len(top_pred_prob) > 0
                    else 1.0,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"GeoCLIP image inference failed: {exc}"
                ) from exc

        elif task == "gps_to_embedding":
            gps_data = features.get("gps_data")
            if gps_data is None:
                raise SchemaValidationError(
                    _MODEL_NAME,
                    ["'gps_data' is required for 'gps_to_embedding' task."],
                )

            if isinstance(gps_data, list) and len(gps_data) > 0:
                if not isinstance(gps_data[0], list):
                    gps_data = [gps_data]
            else:
                raise SchemaValidationError(
                    _MODEL_NAME,
                    ["'gps_data' must be a list of [lat, lon] coordinates."],
                )

            try:
                with torch.no_grad():
                    tensor_gps = torch.Tensor(gps_data).to(self._device)
                    embeddings = self._gps_encoder(tensor_gps)
                    embeddings_np = embeddings.cpu().numpy()

                return PredictionResult(
                    model_name=_MODEL_NAME,
                    model_version=_MODEL_VERSION,
                    prediction={
                        "task": "gps_to_embedding",
                        "embeddings": embeddings_np.tolist(),
                        "shape": list(embeddings_np.shape),
                    },
                    confidence=1.0,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"GeoCLIP GPS embedding generation failed: {exc}"
                ) from exc

        else:
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Unsupported task: '{task}'. Choose either 'image_to_gps' or 'gps_to_embedding'."
                ],
            )

    def input_schema(self) -> dict[str, Any]:
        """Return a JSON Schema dict describing expected input features."""
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "enum": ["image_to_gps", "gps_to_embedding"],
                    "default": "image_to_gps",
                },
                "image_path": {
                    "type": "string",
                    "description": "Local path to the target image (required if task is image_to_gps).",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 5,
                    "description": "Number of top GPS coordinates to return.",
                },
                "gps_data": {
                    "type": "array",
                    "description": "List of [lat, lon] coordinates (required if task is gps_to_embedding).",
                    "items": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {"type": "number"},
                    },
                },
            },
            "required": [],
        }

    def output_schema(self) -> dict[str, Any]:
        """Return a JSON Schema dict describing the prediction output."""
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "predictions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "latitude": {"type": "number"},
                            "longitude": {"type": "number"},
                            "probability": {"type": "number"},
                        },
                        "required": ["latitude", "longitude", "probability"],
                    },
                },
                "embeddings": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                },
                "shape": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["task"],
        }

    def _ensure_loaded(self) -> None:
        if self._model is None or self._gps_encoder is None:
            raise ModelLoadError(
                _MODEL_NAME,
                "Model components are uninitialized. Call load() first.",
            )
