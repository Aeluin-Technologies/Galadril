"""Grounded SAM model for multi-concept promptable segmentation."""

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

_MODEL_NAME = "grounded_sam"
_MODEL_VERSION = "1.1.0"


class GroundedSamModel(BaseModel):
    """Grounded SAM for zero-shot detection and segmentation."""

    def __init__(self) -> None:
        """Initialize the Grounded SAM wrapper."""
        self._detector = None
        self._segmentator = None
        self._processor = None
        self._device: str = "cpu"

    def meta(self) -> ModelMeta:
        """Return the immutable identity of this model."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Grounded SAM model for multi-concept promptable segmentation.",
            tags={
                "domain": "vision",
                "backend": "transformers",
                "framework": "pytorch",
            },
        )

    def load(self, artifact_path: str = "") -> None:
        """Load the Grounding DINO and SAM models."""
        try:
            import torch
            from transformers import (
                AutoModelForMaskGeneration,
                AutoProcessor,
                pipeline,
            )
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "Missing dependencies (torch, torchvision, transformers).",
            ) from exc

        try:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"

            detector_id = "IDEA-Research/grounding-dino-base"
            self._detector = pipeline(
                model=detector_id,
                task="zero-shot-object-detection",
                device=self._device,
            )

            segmenter_id = "facebook/sam-vit-base"
            self._segmentator = AutoModelForMaskGeneration.from_pretrained(
                segmenter_id
            ).to(self._device)
            self._processor = AutoProcessor.from_pretrained(segmenter_id)

            logger.info(
                "model_loaded", model_name=_MODEL_NAME, device=self._device
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release models and GPU memory."""
        self._detector = None
        self._segmentator = None
        self._processor = None
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run object detection followed by segmentation."""
        self._ensure_loaded()
        import torch
        from PIL import Image
        from torchvision.ops import nms

        image_array = self._extract_image(request)
        pil_image = Image.fromarray(image_array).convert("RGB")
        width, height = pil_image.size

        raw_text = request.features.get("text")
        prompts = (
            [p.strip() for p in raw_text.split(".")]
            if isinstance(raw_text, str)
            else raw_text
        )

        return_masks = request.features.get("return_masks", False)
        threshold = request.features.get("threshold", 0.2)
        use_tiling = request.features.get("use_tiling", False)
        tile_size = request.features.get("tile_size", 512)
        tile_overlap = request.features.get("tile_overlap", 0.25)
        nms_threshold = request.features.get("nms_threshold", 0.4)

        if not prompts:
            raise SchemaValidationError(_MODEL_NAME, ["Missing 'text' prompt."])

        labels = [
            label if label.endswith(".") else label + "." for label in prompts
        ]

        try:
            all_detections = []

            if use_tiling:
                stride = int(tile_size * (1 - tile_overlap))
                for y in range(0, height, stride):
                    for x in range(0, width, stride):
                        x2, y2 = (
                            min(x + tile_size, width),
                            min(y + tile_size, height),
                        )
                        tile_img = pil_image.crop((x, y, x2, y2))
                        tile_detections = self._detector(
                            tile_img,
                            candidate_labels=labels,
                            threshold=threshold,
                        )
                        for d in tile_detections:
                            d["box"]["xmin"] += x
                            d["box"]["xmax"] += x
                            d["box"]["ymin"] += y
                            d["box"]["ymax"] += y
                            all_detections.append(d)
            else:
                all_detections = self._detector(
                    pil_image, candidate_labels=labels, threshold=threshold
                )

            if not all_detections:
                return PredictionResult(
                    model_name=_MODEL_NAME,
                    model_version=_MODEL_VERSION,
                    prediction={"total_objects": 0, "concepts": {}},
                    confidence=1.0,
                )

            final_detections = []

            # Group by label to apply NMS per class.
            detections_by_label = {}
            for d in all_detections:
                lbl = d["label"]
                if lbl not in detections_by_label:
                    detections_by_label[lbl] = []
                detections_by_label[lbl].append(d)

            for lbl, dets in detections_by_label.items():
                boxes = torch.tensor(
                    [
                        [
                            d["box"]["xmin"],
                            d["box"]["ymin"],
                            d["box"]["xmax"],
                            d["box"]["ymax"],
                        ]
                        for d in dets
                    ],
                    dtype=torch.float32,
                )
                scores = torch.tensor(
                    [d["score"] for d in dets], dtype=torch.float32
                )

                keep_indices = nms(boxes, scores, iou_threshold=nms_threshold)

                for idx in keep_indices:
                    final_detections.append(dets[idx])

            boxes_for_sam = [
                [
                    [
                        d["box"]["xmin"],
                        d["box"]["ymin"],
                        d["box"]["xmax"],
                        d["box"]["ymax"],
                    ]
                    for d in final_detections
                ]
            ]

            inputs = self._processor(
                images=pil_image, input_boxes=boxes_for_sam, return_tensors="pt"
            ).to(self._device)

            with torch.no_grad():
                outputs = self._segmentator(**inputs)

            masks = self._processor.post_process_masks(
                masks=outputs.pred_masks,
                original_sizes=inputs.original_sizes,
                reshaped_input_sizes=inputs.reshaped_input_sizes,
            )[0]

            masks = masks.cpu().float()
            masks = masks.permute(0, 2, 3, 1)
            masks = masks.mean(axis=-1)
            masks = (masks > 0).int().numpy().astype(np.uint8)

            structured_output = {}
            total_count = len(final_detections)

            for idx, detection in enumerate(final_detections):
                label = detection["label"].rstrip(".")
                if label not in structured_output:
                    structured_output[label] = {"count": 0, "instances": []}

                structured_output[label]["count"] += 1
                structured_output[label]["instances"].append(
                    {
                        "score": float(detection["score"]),
                        "box": [
                            detection["box"]["xmin"],
                            detection["box"]["ymin"],
                            detection["box"]["xmax"],
                            detection["box"]["ymax"],
                        ],
                        "mask": masks[idx].tolist() if return_masks else None,
                    }
                )

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={
                    "total_objects": total_count,
                    "concepts": structured_output,
                },
                confidence=1.0,
            )
        except Exception as exc:
            raise RuntimeError(f"Grounded SAM inference failed: {exc}") from exc

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["image", "text"],
            "properties": {
                "image": {"type": "ndarray"},
                "text": {"type": "string"},
                "threshold": {"type": "number", "default": 0.2},
                "use_tiling": {"type": "boolean", "default": False},
                "tile_size": {"type": "integer", "default": 512},
                "tile_overlap": {"type": "number", "default": 0.25},
                "nms_threshold": {"type": "number", "default": 0.4},
                "return_masks": {"type": "boolean", "default": False},
            },
        }

    def output_schema(self) -> dict[str, Any]:
        """(Same schema as before)"""
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
                                        "mask": {"type": "array"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }

    def _ensure_loaded(self) -> None:
        if self._detector is None or self._segmentator is None:
            raise ModelLoadError(_MODEL_NAME, "Models are not loaded.")

    @staticmethod
    def _extract_image(request: PredictionRequest) -> NDArray[np.uint8]:
        image = request.features.get("image")
        if image is None or not isinstance(image, np.ndarray):
            raise SchemaValidationError(_MODEL_NAME, ["Invalid image format."])
        return image
