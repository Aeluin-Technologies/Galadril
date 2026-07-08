"""SigLIP 2 image and text embedding model for vector search."""

from __future__ import annotations

from enum import StrEnum, unique
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from numpy.typing import NDArray

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

_MODEL_NAME = "siglip2"
_MODEL_VERSION = "2.0.0"


@unique
class SigLIPAction(StrEnum):
    """Supported inference actions for the SigLIP model."""

    EMBED_IMAGE = "embed_image"
    EMBED_TEXT = "embed_text"


class SigLIPModel(BaseModel):
    """Google SigLIP 2 model for multimodal feature extraction."""

    def __init__(self) -> None:
        self._model: Any | None = None
        self._processor: Any | None = None
        self._device: str = "cpu"

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Extracts normalized image and text embeddings using google/siglip2-so400m-patch16-naflex.",
            tags={
                "domain": "multimodal",
                "backend": "transformers",
                "framework": "pytorch",
            },
            deprecated=True,
        )

    def load(self, artifact_path: str) -> None:
        """Load the SigLIP 2 model and processor."""
        try:
            import torch
            from transformers import AutoModel, AutoProcessor
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "transformers or torch is not installed.",
            ) from exc

        try:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"

            self._model = AutoModel.from_pretrained(
                artifact_path,
                device_map=self._device,
                dtype=torch.float16
                if self._device == "cuda"
                else torch.float32,
            ).eval()
            self._processor = AutoProcessor.from_pretrained(artifact_path)

            logger.info(
                "model_loaded", model_name=_MODEL_NAME, device=self._device
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str) -> None:
        """Download the SigLIP 2 checkpoint into target_path."""
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
                repo_id="google/siglip2-so400m-patch16-naflex",
                local_dir=target_path,
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release the model from memory."""
        self._model = None
        self._processor = None
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        logger.info("model_cleaned_up", model_name=_MODEL_NAME)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Dispatch to the appropriate action handler."""
        self._ensure_loaded()
        action = self._extract_action(request)

        match action:
            case SigLIPAction.EMBED_IMAGE:
                return self._predict_embed_image(request)
            case SigLIPAction.EMBED_TEXT:
                return self._predict_embed_text(request)

    def _predict_embed_image(
        self, request: PredictionRequest
    ) -> PredictionResult:
        import torch
        import torch.nn.functional as F

        image = self._extract_image(request, key="image")

        try:
            inputs = self._processor(images=[image], return_tensors="pt").to(
                self._device
            )

            with torch.no_grad():
                image_features = self._model.get_image_features(**inputs)

                if hasattr(image_features, "pooler_output"):
                    image_features = image_features.pooler_output
                elif hasattr(image_features, "image_embeds"):
                    image_features = image_features.image_embeds
                elif isinstance(image_features, tuple):
                    image_features = image_features[0]

                image_features = F.normalize(image_features, p=2, dim=-1)

            embedding_list = image_features[0].cpu().numpy().tolist()

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={
                    "embedding": embedding_list,
                    "embedding_dim": len(embedding_list),
                    "type": "image",
                },
                confidence=1.0,
            )
        except Exception as exc:
            raise RuntimeError(f"SigLIP image inference failed: {exc}") from exc

    def _predict_embed_text(
        self, request: PredictionRequest
    ) -> PredictionResult:
        import torch
        import torch.nn.functional as F

        text = request.features.get("text")
        if not text or not isinstance(text, str):
            raise SchemaValidationError(
                _MODEL_NAME, ["Feature 'text' must be a non-empty string."]
            )

        try:
            inputs = self._processor(
                text=[text], padding="max_length", return_tensors="pt"
            ).to(self._device)

            with torch.no_grad():
                text_features = self._model.get_text_features(**inputs)

                if hasattr(text_features, "pooler_output"):
                    text_features = text_features.pooler_output
                elif hasattr(text_features, "text_embeds"):
                    text_features = text_features.text_embeds
                elif isinstance(text_features, tuple):
                    text_features = text_features[0]

                text_features = F.normalize(text_features, p=2, dim=-1)

            embedding_list = text_features[0].cpu().numpy().tolist()

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={
                    "embedding": embedding_list,
                    "embedding_dim": len(embedding_list),
                    "type": "text",
                },
                confidence=1.0,
            )
        except Exception as exc:
            raise RuntimeError(f"SigLIP text inference failed: {exc}") from exc

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [a.value for a in SigLIPAction],
                },
                "image": {
                    "type": "ndarray",
                    "description": "RGB image as a numpy array. Required if action is embed_image.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to embed. Required if action is embed_text.",
                },
            },
        }

    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "embedding": {
                    "type": "array",
                    "items": {"type": "number"},
                },
                "embedding_dim": {"type": "integer"},
                "type": {"type": "string"},
            },
        }

    def _ensure_loaded(self) -> None:
        if self._model is None or self._processor is None:
            raise ModelLoadError(_MODEL_NAME, "Model is not loaded.")

    @staticmethod
    def _extract_action(request: PredictionRequest) -> SigLIPAction:
        raw_action = request.features.get("action")
        if raw_action is None:
            raise SchemaValidationError(
                _MODEL_NAME, ["Missing required feature: 'action'."]
            )
        try:
            return SigLIPAction(raw_action)
        except ValueError as exc:
            raise SchemaValidationError(
                _MODEL_NAME, [f"Invalid action '{raw_action}'."]
            ) from exc

    @staticmethod
    def _extract_image(
        request: PredictionRequest, *, key: str
    ) -> NDArray[np.uint8]:
        image = request.features.get(key)
        if image is None:
            raise SchemaValidationError(
                _MODEL_NAME, [f"Missing required feature: '{key}'."]
            )
        if not isinstance(image, np.ndarray):
            raise SchemaValidationError(
                _MODEL_NAME, ["Image must be a numpy array."]
            )
        return image
