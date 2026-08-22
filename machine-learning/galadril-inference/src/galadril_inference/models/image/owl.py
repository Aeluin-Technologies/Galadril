"""OwlV2 model for zero-shot object detection."""

from __future__ import annotations

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
from galadril_inference.models.runtime import create_session

logger = structlog.get_logger(__name__)

_MODEL_NAME = "owlv2"
_MODEL_VERSION = "1.1.0"
_ONNX_REPO = "onnx-community/owlv2-base-patch16-ensemble-ONNX"


class OwlV2Model(BaseModel):
    """OwlV2 for zero-shot detection."""

    def __init__(self) -> None:
        """Initialize the OwlV2 wrapper."""
        self._session: Any | None = None
        self._processor: Any | None = None

    def meta(self) -> ModelMeta:
        """Return the immutable identity of this model."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="OwlV2 model for zero-shot object detection.",
            tags={
                "domain": "vision",
                "backend": "onnxruntime",
                "framework": "onnx",
            },
        )

    def load(
        self,
        artifact_path: str = "",
        compute_type: str = "int8",
        device: str = "auto",
    ) -> None:
        """Load the quantized OwlV2 ONNX detector."""
        try:
            from transformers import Owlv2Processor
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "transformers is required for OwlV2 preprocessing.",
            ) from exc

        try:
            root = Path(artifact_path)
            suffix = self._compute_suffix(compute_type)
            model_path = root / "onnx" / f"model{suffix}.onnx"
            if not model_path.is_file():
                raise FileNotFoundError(
                    "OwlV2 ONNX artifact is missing. Call download() with "
                    f"compute_type='{compute_type}' before load()."
                )
            self._processor = Owlv2Processor.from_pretrained(
                str(root), local_files_only=True
            )
            self._session = create_session(model_path, device=device)

            logger.info(
                "model_loaded",
                model_name=_MODEL_NAME,
                compute_type=compute_type,
                providers=self._session.get_providers(),
            )
        except Exception as exc:
            self.cleanup()
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str, compute_type: str = "int8") -> None:
        """Download only one optimized OwlV2 ONNX graph and its processor."""
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "huggingface_hub is not installed.",
            ) from exc

        try:
            Path(target_path).mkdir(parents=True, exist_ok=True)
            suffix = self._compute_suffix(compute_type)
            snapshot_download(
                repo_id=_ONNX_REPO,
                local_dir=target_path,
                allow_patterns=[
                    "*.json",
                    "merges.txt",
                    "vocab.json",
                    f"onnx/model{suffix}.onnx",
                ],
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release models and GPU memory."""
        self._session = None
        self._processor = None

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run object detection."""
        self._ensure_loaded()
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
                text=[labels], images=pil_image, return_tensors="np"
            )
            input_names = {value.name for value in self._session.get_inputs()}
            feeds = {
                key: np.ascontiguousarray(value)
                for key, value in inputs.items()
                if key in input_names
            }
            output_meta = self._session.get_outputs()
            output_values = self._session.run(None, feeds)
            outputs = {
                meta.name: value
                for meta, value in zip(output_meta, output_values, strict=True)
            }
            logits = np.asarray(outputs["logits"])[0]
            pred_boxes = np.asarray(outputs["pred_boxes"])[0]
            class_indices = np.argmax(logits, axis=-1)
            max_logits = np.max(logits, axis=-1)
            scores_array = 1.0 / (1.0 + np.exp(-max_logits))
            keep = scores_array >= float(threshold)
            boxes_array = self._scale_boxes(
                pred_boxes[keep], pil_image.width, pil_image.height
            )
            scores = scores_array[keep].tolist()
            text_labels = [labels[index] for index in class_indices[keep]]

            structured_output = {}
            for box, score, label in zip(
                boxes_array, scores, text_labels, strict=False
            ):
                if label not in structured_output:
                    structured_output[label] = {"count": 0, "instances": []}

                structured_output[label]["count"] += 1
                structured_output[label]["instances"].append(
                    {
                        "score": float(score),
                        "box": [float(value) for value in box],
                        "mask": None,
                    }
                )

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={
                    "total_objects": len(boxes_array),
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
        if self._session is None or self._processor is None:
            raise ModelLoadError(_MODEL_NAME, "Models are not loaded.")

    @staticmethod
    def _compute_suffix(compute_type: str) -> str:
        """Map a precision name to the ONNX Community file suffix."""
        suffixes = {
            "float32": "",
            "fp32": "",
            "float16": "_fp16",
            "fp16": "_fp16",
            "int8": "_int8",
            "uint8": "_uint8",
            "q4": "_q4",
            "int4": "_q4",
            "q4f16": "_q4f16",
        }
        try:
            return suffixes[compute_type.lower()]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported OwlV2 compute type '{compute_type}'."
            ) from exc

    @staticmethod
    def _scale_boxes(
        boxes: NDArray[np.floating[Any]], width: int, height: int
    ) -> NDArray[np.float32]:
        """Convert normalized center boxes into clamped pixel corner boxes."""
        if boxes.size == 0:
            return np.empty((0, 4), dtype=np.float32)
        centers = np.asarray(boxes, dtype=np.float32)
        corners = np.empty_like(centers)
        half_width = centers[:, 2] * 0.5
        half_height = centers[:, 3] * 0.5
        corners[:, 0] = (centers[:, 0] - half_width) * width
        corners[:, 1] = (centers[:, 1] - half_height) * height
        corners[:, 2] = (centers[:, 0] + half_width) * width
        corners[:, 3] = (centers[:, 1] + half_height) * height
        corners[:, (0, 2)] = np.clip(corners[:, (0, 2)], 0, width)
        corners[:, (1, 3)] = np.clip(corners[:, (1, 3)], 0, height)
        return corners

    @staticmethod
    def _extract_image(request: PredictionRequest) -> NDArray[np.uint8]:
        image = request.features.get("image")
        if image is None or not isinstance(image, np.ndarray):
            raise SchemaValidationError(_MODEL_NAME, ["Invalid image format."])
        return image
