"""OwlV2 model for zero-shot object detection."""

from __future__ import annotations

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

_MODEL_NAME = "owlv2"
_MODEL_VERSION = "1.0.0"


class OwlV2Model(BaseModel):
    """OwlV2 for zero-shot detection."""

    def __init__(self) -> None:
        """Initialize the OwlV2 wrapper."""
        self._model = None
        self._processor = None
        self._device: str = "cpu"

    def meta(self) -> ModelMeta:
        """Return the immutable identity of this model."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="OwlV2 model for zero-shot object detection.",
            tags={
                "domain": "vision",
                "backend": "transformers",
                "framework": "pytorch",
            },
        )

    def load(self, artifact_path: str = "") -> None:
        """Load the OwlV2 model."""
        try:
            import torch
            from transformers import Owlv2ForObjectDetection, Owlv2Processor
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "Missing dependencies (torch, transformers).",
            ) from exc

        try:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            model_id = "google/owlv2-large-patch14"

            self._processor = Owlv2Processor.from_pretrained(model_id)
            self._model = Owlv2ForObjectDetection.from_pretrained(model_id).to(
                self._device
            )

            logger.info(
                "model_loaded", model_name=_MODEL_NAME, device=self._device
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release models and GPU memory."""
        self._model = None
        self._processor = None
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run object detection."""
        self._ensure_loaded()
        import torch
        from PIL import Image

        image_array = self._extract_image(request)
        pil_image = Image.fromarray(image_array).convert("RGB")

        raw_text = request.features.get("text")
        if not raw_text:
            raise SchemaValidationError(_MODEL_NAME, ["Missing 'text' prompt."])

        labels = [p.strip() for p in raw_text.split(".") if p.strip()]
        threshold = request.features.get("threshold", 0.1)

        try:
            inputs = self._processor(
                text=[labels], images=pil_image, return_tensors="pt"
            ).to(self._device)

            with torch.no_grad():
                outputs = self._model(**inputs)

            target_sizes = torch.tensor(
                [(pil_image.height, pil_image.width)]
            ).to(self._device)

            results = self._processor.post_process_grounded_object_detection(
                outputs=outputs,
                target_sizes=target_sizes,
                threshold=threshold,
                text_labels=[labels],
            )[0]

            boxes = results["boxes"].cpu().tolist()
            scores = results["scores"].cpu().tolist()
            text_labels = results["text_labels"]

            structured_output = {}
            for box, score, label in zip(boxes, scores, text_labels):
                if label not in structured_output:
                    structured_output[label] = {"count": 0, "instances": []}

                structured_output[label]["count"] += 1
                structured_output[label]["instances"].append(
                    {
                        "score": float(score),
                        "box": [box[0], box[1], box[2], box[3]],
                        "mask": None,
                    }
                )

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={
                    "total_objects": len(boxes),
                    "concepts": structured_output,
                },
                confidence=1.0,
            )
        except Exception as exc:
            raise RuntimeError(f"OwlV2 inference failed: {exc}") from exc

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["image", "text"],
            "properties": {
                "image": {"type": "ndarray"},
                "text": {"type": "string"},
                "threshold": {"type": "number", "default": 0.1},
            },
        }

    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "total_objects": {"type": "integer"},
                "concepts": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "count": {"type": "integer"},
                            "instances": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "score": {"type": "number"},
                                        "box": {
                                            "type": "array",
                                            "items": {"type": "number"},
                                        },
                                        "mask": {"type": ["array", "null"]},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }

    def _ensure_loaded(self) -> None:
        if self._model is None or self._processor is None:
            raise ModelLoadError(_MODEL_NAME, "Models are not loaded.")

    @staticmethod
    def _extract_image(request: PredictionRequest) -> NDArray[np.uint8]:
        image = request.features.get("image")
        if image is None or not isinstance(image, np.ndarray):
            raise SchemaValidationError(_MODEL_NAME, ["Invalid image format."])
        return image
