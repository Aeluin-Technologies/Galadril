"""Grounded SAM detection and segmentation through quantized ONNX graphs."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Final

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

_MODEL_NAME: Final = "grounded_sam"
_MODEL_VERSION: Final = "1.2.0"
_DETECTOR_REPO: Final = "onnx-community/grounding-dino-tiny-ONNX"
_SEGMENTER_REPO: Final = "onnx-community/sam-vit-base-ONNX"


class GroundedSamModel(BaseModel):
    """Zero-shot object detection with optional SAM mask generation."""

    def __init__(self) -> None:
        self._detector_session: Any | None = None
        self._vision_session: Any | None = None
        self._prompt_session: Any | None = None
        self._detector_processor: Any | None = None
        self._segmenter_processor: Any | None = None

    def meta(self) -> ModelMeta:
        """Return the immutable identity of this model."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Quantized Grounding DINO and SAM for promptable segmentation.",
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
        """Load optimized detector, vision encoder, and prompt decoder graphs."""
        try:
            from transformers import AutoProcessor
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "transformers is required for Grounded SAM preprocessing.",
            ) from exc

        try:
            root = Path(artifact_path)
            detector_root = root / "grounding-dino"
            segmenter_root = root / "sam-vit-base"
            suffix = self._compute_suffix(compute_type)
            detector_path = detector_root / "onnx" / f"model{suffix}.onnx"
            vision_path = (
                segmenter_root / "onnx" / f"vision_encoder{suffix}.onnx"
            )
            prompt_path = (
                segmenter_root
                / "onnx"
                / f"prompt_encoder_mask_decoder{suffix}.onnx"
            )
            missing = [
                str(path)
                for path in (detector_path, vision_path, prompt_path)
                if not path.is_file()
            ]
            if missing:
                raise FileNotFoundError(
                    "Grounded SAM ONNX artifacts are missing: "
                    + ", ".join(missing)
                )

            self._detector_processor = AutoProcessor.from_pretrained(
                str(detector_root), local_files_only=True
            )
            self._segmenter_processor = AutoProcessor.from_pretrained(
                str(segmenter_root), local_files_only=True
            )
            self._detector_session = create_session(
                detector_path, device=device
            )
            self._vision_session = create_session(vision_path, device=device)
            self._prompt_session = create_session(prompt_path, device=device)

            logger.info(
                "model_loaded",
                model_name=_MODEL_NAME,
                compute_type=compute_type,
                detector_providers=self._detector_session.get_providers(),
                segmenter_providers=self._vision_session.get_providers(),
            )
        except Exception as exc:
            self.cleanup()
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str, compute_type: str = "int8") -> None:
        """Download only the selected ONNX precision and processor assets."""
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME, "huggingface_hub is not installed."
            ) from exc

        root = Path(target_path)
        detector_dir = root / "grounding-dino"
        segmenter_dir = root / "sam-vit-base"
        suffix = self._compute_suffix(compute_type)

        try:
            detector_dir.mkdir(parents=True, exist_ok=True)
            segmenter_dir.mkdir(parents=True, exist_ok=True)
            snapshot_download(
                repo_id=_DETECTOR_REPO,
                local_dir=str(detector_dir),
                allow_patterns=[
                    "*.json",
                    "vocab.txt",
                    f"onnx/model{suffix}.onnx",
                ],
            )
            snapshot_download(
                repo_id=_SEGMENTER_REPO,
                local_dir=str(segmenter_dir),
                allow_patterns=[
                    "*.json",
                    f"onnx/vision_encoder{suffix}.onnx",
                    f"onnx/prompt_encoder_mask_decoder{suffix}.onnx",
                ],
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release native sessions and processor state."""
        self._detector_session = None
        self._vision_session = None
        self._prompt_session = None
        self._detector_processor = None
        self._segmenter_processor = None

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Detect prompted concepts and optionally generate instance masks."""
        self._ensure_loaded()
        from PIL import Image

        image = self._extract_image(request)
        pil_image = Image.fromarray(image).convert("RGB")
        labels = self._extract_labels(request.features.get("text"))
        threshold = float(request.features.get("threshold", 0.2))
        return_masks = bool(request.features.get("return_masks", False))
        use_tiling = bool(request.features.get("use_tiling", False))
        tile_size = int(request.features.get("tile_size", 512))
        tile_overlap = float(request.features.get("tile_overlap", 0.25))
        nms_threshold = float(request.features.get("nms_threshold", 0.4))

        if not 0.0 <= threshold <= 1.0:
            raise SchemaValidationError(
                _MODEL_NAME, ["Feature 'threshold' must be between 0 and 1."]
            )
        if not 0.0 <= tile_overlap < 1.0:
            raise SchemaValidationError(
                _MODEL_NAME, ["Feature 'tile_overlap' must be in [0, 1)."]
            )
        if tile_size < 1:
            raise SchemaValidationError(
                _MODEL_NAME, ["Feature 'tile_size' must be positive."]
            )

        try:
            detections = self._detect(
                pil_image,
                labels,
                threshold=threshold,
                use_tiling=use_tiling,
                tile_size=tile_size,
                tile_overlap=tile_overlap,
            )
            detections = self._nms_by_label(detections, nms_threshold)
            if not detections:
                return self._result({"total_objects": 0, "concepts": {}})

            masks: list[NDArray[np.uint8]] | None = None
            if return_masks:
                boxes = np.asarray(
                    [detection["box"] for detection in detections],
                    dtype=np.float32,
                )
                masks = self._segment(pil_image, boxes)

            concepts: dict[str, dict[str, Any]] = {}
            for index, detection in enumerate(detections):
                label = str(detection["label"])
                concept = concepts.setdefault(
                    label, {"count": 0, "instances": []}
                )
                concept["count"] += 1
                concept["instances"].append(
                    {
                        "score": float(detection["score"]),
                        "box": [float(value) for value in detection["box"]],
                        "mask": masks[index].tolist()
                        if masks is not None
                        else None,
                    }
                )

            return self._result(
                {"total_objects": len(detections), "concepts": concepts}
            )
        except SchemaValidationError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Grounded SAM inference failed: {exc}") from exc

    def _detect(
        self,
        image: Any,
        labels: list[str],
        *,
        threshold: float,
        use_tiling: bool,
        tile_size: int,
        tile_overlap: float,
    ) -> list[dict[str, Any]]:
        """Run detection over one image or overlapping tiles."""
        if not use_tiling:
            return self._detect_tile(image, labels, threshold, 0, 0)

        width, height = image.size
        stride = max(1, int(tile_size * (1.0 - tile_overlap)))
        detections: list[dict[str, Any]] = []
        for y in range(0, height, stride):
            for x in range(0, width, stride):
                right = min(x + tile_size, width)
                bottom = min(y + tile_size, height)
                tile = image.crop((x, y, right, bottom))
                detections.extend(
                    self._detect_tile(tile, labels, threshold, x, y)
                )
        return detections

    def _detect_tile(
        self,
        image: Any,
        labels: list[str],
        threshold: float,
        offset_x: int,
        offset_y: int,
    ) -> list[dict[str, Any]]:
        """Run Grounding DINO and map token logits back to prompt labels."""
        prompt = ". ".join(label.rstrip(".") for label in labels) + "."
        values = self._detector_processor(
            images=image, text=prompt, return_tensors="np"
        )
        feeds = self._session_feeds(self._detector_session, values)
        output_meta = self._detector_session.get_outputs()
        output_values = self._detector_session.run(None, feeds)
        outputs = {
            meta.name: value
            for meta, value in zip(output_meta, output_values, strict=True)
        }
        logits = np.asarray(outputs["logits"], dtype=np.float32)[0]
        boxes = np.asarray(outputs["pred_boxes"], dtype=np.float32)[0]
        input_ids = np.asarray(values["input_ids"])[0]
        token_spans = self._label_token_spans(input_ids, labels)

        label_scores = np.empty(
            (logits.shape[0], len(labels)), dtype=np.float32
        )
        for index, positions in enumerate(token_spans):
            label_scores[:, index] = np.max(logits[:, positions], axis=1)
        label_scores = 1.0 / (1.0 + np.exp(-label_scores))
        label_indices = np.argmax(label_scores, axis=1)
        scores = label_scores[np.arange(label_scores.shape[0]), label_indices]
        keep = scores >= threshold
        pixel_boxes = self._scale_boxes(boxes[keep], image.width, image.height)
        pixel_boxes[:, (0, 2)] += offset_x
        pixel_boxes[:, (1, 3)] += offset_y

        return [
            {
                "box": box,
                "score": float(score),
                "label": labels[int(label_index)].rstrip("."),
            }
            for box, score, label_index in zip(
                pixel_boxes, scores[keep], label_indices[keep], strict=True
            )
        ]

    def _label_token_spans(
        self, input_ids: NDArray[np.integer[Any]], labels: list[str]
    ) -> list[NDArray[np.int64]]:
        """Find each candidate label's token positions inside the combined prompt."""
        tokenizer = self._detector_processor.tokenizer
        spans: list[NDArray[np.int64]] = []
        cursor = 0
        ids = input_ids.tolist()
        for label in labels:
            encoded = tokenizer(label.rstrip("."), add_special_tokens=False)[
                "input_ids"
            ]
            positions = self._find_subsequence(ids, encoded, cursor)
            if positions is None:
                raise RuntimeError(
                    f"Unable to align detector tokens for '{label}'."
                )
            start, stop = positions
            spans.append(np.arange(start, stop, dtype=np.int64))
            cursor = stop
        return spans

    def _segment(
        self, image: Any, boxes: NDArray[np.float32]
    ) -> list[NDArray[np.uint8]]:
        """Encode an image once and decode a mask for every detected box."""
        values = self._segmenter_processor(images=image, return_tensors="np")
        vision_feeds = self._session_feeds(self._vision_session, values)
        vision_meta = self._vision_session.get_outputs()
        vision_values = self._vision_session.run(None, vision_feeds)
        vision_outputs = {
            meta.name: value
            for meta, value in zip(vision_meta, vision_values, strict=True)
        }

        original_size = np.asarray(values["original_sizes"])[0]
        reshaped_size = np.asarray(values["reshaped_input_sizes"])[0]
        scale = np.asarray(
            [
                reshaped_size[1] / original_size[1],
                reshaped_size[0] / original_size[0],
            ],
            dtype=np.float32,
        )
        points = boxes.reshape(1, boxes.shape[0], 2, 2).copy()
        points[..., 0] *= scale[0]
        points[..., 1] *= scale[1]
        labels = np.broadcast_to(
            np.asarray([2, 3], dtype=np.int64),
            (1, boxes.shape[0], 2),
        )
        prompt_feeds = {
            "input_points": np.ascontiguousarray(points),
            "input_labels": np.ascontiguousarray(labels),
            "image_embeddings": vision_outputs["image_embeddings"],
            "image_positional_embeddings": vision_outputs[
                "image_positional_embeddings"
            ],
        }
        prompt_meta = self._prompt_session.get_outputs()
        prompt_values = self._prompt_session.run(None, prompt_feeds)
        prompt_outputs = {
            meta.name: value
            for meta, value in zip(prompt_meta, prompt_values, strict=True)
        }
        iou_scores = np.asarray(prompt_outputs["iou_scores"])[0]
        candidates = np.asarray(prompt_outputs["pred_masks"])[0]
        best = np.argmax(iou_scores, axis=-1)
        selected = candidates[np.arange(candidates.shape[0]), best]
        return self._resize_masks(selected, reshaped_size, original_size)

    @staticmethod
    def _resize_masks(
        masks: NDArray[np.floating[Any]],
        reshaped_size: NDArray[np.integer[Any]],
        original_size: NDArray[np.integer[Any]],
    ) -> list[NDArray[np.uint8]]:
        """Remove SAM padding and resize low-resolution logits to input geometry."""
        from PIL import Image

        mask_height, mask_width = masks.shape[-2:]
        crop_height = max(
            1, round(float(reshaped_size[0]) / 1024 * mask_height)
        )
        crop_width = max(1, round(float(reshaped_size[1]) / 1024 * mask_width))
        output_size = (int(original_size[1]), int(original_size[0]))
        resized: list[NDArray[np.uint8]] = []
        for mask in masks:
            cropped = np.ascontiguousarray(mask[:crop_height, :crop_width])
            image = Image.fromarray(cropped.astype(np.float32), mode="F")
            image = image.resize(
                output_size, resample=Image.Resampling.BILINEAR
            )
            resized.append((np.asarray(image) > 0.0).astype(np.uint8))
        return resized

    @classmethod
    def _nms_by_label(
        cls, detections: list[dict[str, Any]], threshold: float
    ) -> list[dict[str, Any]]:
        """Apply allocation-conscious NumPy NMS independently per concept."""
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for detection in detections:
            grouped[str(detection["label"])].append(detection)

        kept: list[dict[str, Any]] = []
        for group in grouped.values():
            boxes = np.asarray(
                [item["box"] for item in group], dtype=np.float32
            )
            scores = np.asarray(
                [item["score"] for item in group], dtype=np.float32
            )
            kept.extend(
                group[index] for index in cls._nms(boxes, scores, threshold)
            )
        return kept

    @staticmethod
    def _nms(
        boxes: NDArray[np.float32],
        scores: NDArray[np.float32],
        threshold: float,
    ) -> list[int]:
        """Return indices retained by greedy intersection-over-union suppression."""
        if boxes.size == 0:
            return []
        x1, y1, x2, y2 = boxes.T
        areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        order = np.argsort(scores)[::-1]
        keep: list[int] = []
        while order.size:
            current = int(order[0])
            keep.append(current)
            if order.size == 1:
                break
            remaining = order[1:]
            intersection_width = np.maximum(
                0.0,
                np.minimum(x2[current], x2[remaining])
                - np.maximum(x1[current], x1[remaining]),
            )
            intersection_height = np.maximum(
                0.0,
                np.minimum(y2[current], y2[remaining])
                - np.maximum(y1[current], y1[remaining]),
            )
            intersection = intersection_width * intersection_height
            union = areas[current] + areas[remaining] - intersection
            iou = np.divide(
                intersection,
                union,
                out=np.zeros_like(intersection),
                where=union > 0.0,
            )
            order = remaining[iou <= threshold]
        return keep

    @staticmethod
    def _scale_boxes(
        boxes: NDArray[np.float32], width: int, height: int
    ) -> NDArray[np.float32]:
        """Convert normalized center boxes to clamped pixel corner boxes."""
        if boxes.size == 0:
            return np.empty((0, 4), dtype=np.float32)
        corners = np.empty_like(boxes)
        half_width = boxes[:, 2] * 0.5
        half_height = boxes[:, 3] * 0.5
        corners[:, 0] = (boxes[:, 0] - half_width) * width
        corners[:, 1] = (boxes[:, 1] - half_height) * height
        corners[:, 2] = (boxes[:, 0] + half_width) * width
        corners[:, 3] = (boxes[:, 1] + half_height) * height
        corners[:, (0, 2)] = np.clip(corners[:, (0, 2)], 0, width)
        corners[:, (1, 3)] = np.clip(corners[:, (1, 3)], 0, height)
        return corners

    @staticmethod
    def _find_subsequence(
        values: list[int], expected: list[int], start: int
    ) -> tuple[int, int] | None:
        """Find a token sequence without allocating sliding windows."""
        if not expected:
            return None
        stop = len(values) - len(expected) + 1
        for index in range(start, stop):
            if values[index : index + len(expected)] == expected:
                return index, index + len(expected)
        return None

    @staticmethod
    def _session_feeds(session: Any, values: dict[str, Any]) -> dict[str, Any]:
        """Filter processor outputs to graph inputs and ensure contiguous buffers."""
        input_names = {value.name for value in session.get_inputs()}
        return {
            key: np.ascontiguousarray(value)
            for key, value in values.items()
            if key in input_names
        }

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
                f"Unsupported Grounded SAM compute type '{compute_type}'."
            ) from exc

    @staticmethod
    def _extract_labels(raw_text: Any) -> list[str]:
        """Validate a period-delimited string or explicit prompt list."""
        if isinstance(raw_text, str):
            labels = [
                value.strip() for value in raw_text.split(".") if value.strip()
            ]
        elif isinstance(raw_text, list):
            labels = [
                value.strip()
                for value in raw_text
                if isinstance(value, str) and value.strip()
            ]
        else:
            labels = []
        if not labels:
            raise SchemaValidationError(_MODEL_NAME, ["Missing 'text' prompt."])
        return labels

    @staticmethod
    def _extract_image(request: PredictionRequest) -> NDArray[np.uint8]:
        """Validate and return an RGB image buffer."""
        image = request.features.get("image")
        if not isinstance(image, np.ndarray):
            raise SchemaValidationError(_MODEL_NAME, ["Invalid image format."])
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
            raise SchemaValidationError(
                _MODEL_NAME,
                ["Image must be a uint8 array with shape (H, W, 3)."],
            )
        return image

    def _ensure_loaded(self) -> None:
        """Raise when any stage of the ONNX pipeline is unavailable."""
        if any(
            value is None
            for value in (
                self._detector_session,
                self._vision_session,
                self._prompt_session,
                self._detector_processor,
                self._segmenter_processor,
            )
        ):
            raise ModelLoadError(_MODEL_NAME, "Models are not loaded.")

    @staticmethod
    def _result(prediction: dict[str, Any]) -> PredictionResult:
        """Build a successful prediction result."""
        return PredictionResult(
            model_name=_MODEL_NAME,
            model_version=_MODEL_VERSION,
            prediction=prediction,
            confidence=1.0,
        )

    def input_schema(self) -> dict[str, Any]:
        """Return the JSON input schema."""
        return {
            "type": "object",
            "required": ["image", "text"],
            "properties": {
                "image": {"type": "ndarray"},
                "text": {"type": ["string", "array"]},
                "threshold": {"type": "number", "default": 0.2},
                "use_tiling": {"type": "boolean", "default": False},
                "tile_size": {"type": "integer", "default": 512},
                "tile_overlap": {"type": "number", "default": 0.25},
                "nms_threshold": {"type": "number", "default": 0.4},
                "return_masks": {"type": "boolean", "default": False},
            },
        }

    def output_schema(self) -> dict[str, Any]:
        """Return the JSON output schema."""
        return {
            "type": "object",
            "properties": {
                "total_objects": {"type": "integer"},
                "concepts": {"type": "object"},
            },
            "required": ["total_objects", "concepts"],
        }
