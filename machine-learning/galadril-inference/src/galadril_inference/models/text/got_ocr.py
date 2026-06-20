"""GOT-OCR 2.0 model for unified end-to-end OCR."""

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

_MODEL_NAME = "got_ocr"
_MODEL_VERSION = "2.0.0"


@unique
class GotOcrAction(StrEnum):
    """Supported inference actions for the GOT-OCR model."""

    SINGLE_PAGE = "single_page"
    MULTI_PAGE = "multi_page"
    REGION = "region"


class GotOcrModel(BaseModel):
    """StepFun GOT-OCR 2.0 model for image-to-text formatting."""

    def __init__(self) -> None:
        """Initialize GOT-OCR model wrapper."""
        self._model: Any | None = None
        self._processor: Any | None = None
        self._device: str = "cpu"

    def meta(self) -> ModelMeta:
        """Return the immutable identity of this model."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Unified OCR model capable of formatted text, multi-page, and region extraction.",
            tags={
                "domain": "ocr",
                "backend": "transformers",
                "framework": "pytorch",
            },
        )

    def load(self, artifact_path: str = "stepfun-ai/GOT-OCR-2.0-hf") -> None:
        """Load the GOT-OCR model and processor."""
        try:
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "transformers or torch is not installed.",
            ) from exc

        try:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            torch_dtype = (
                torch.bfloat16 if self._device == "cuda" else torch.float32
            )

            self._model = AutoModelForImageTextToText.from_pretrained(
                artifact_path,
                torch_dtype=torch_dtype,
                device_map=self._device,
            ).eval()
            self._processor = AutoProcessor.from_pretrained(artifact_path)

            logger.info(
                "model_loaded", model_name=_MODEL_NAME, device=self._device
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str) -> None:
        """Download the GOT-OCR checkpoint into target_path."""
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
                repo_id="stepfun-ai/GOT-OCR-2.0-hf",
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
            case GotOcrAction.SINGLE_PAGE:
                return self._predict_single_page(request)
            case GotOcrAction.MULTI_PAGE:
                return self._predict_multi_page(request)
            case GotOcrAction.REGION:
                return self._predict_region(request)

    def _predict_single_page(
        self, request: PredictionRequest
    ) -> PredictionResult:
        """Run standard or formatted OCR on a single image."""
        from PIL import Image

        image = self._extract_image(request, key="image")
        format_text = request.features.get("format", True)

        try:
            pil_image = Image.fromarray(image).convert("RGB")
            inputs = self._processor(
                pil_image, return_tensors="pt", format=format_text
            ).to(self._device)
            return self._generate_text(inputs)
        except Exception as exc:
            raise RuntimeError(
                f"GOT-OCR single page inference failed: {exc}"
            ) from exc

    def _predict_multi_page(
        self, request: PredictionRequest
    ) -> PredictionResult:
        """Run formatted OCR continuously across multiple images."""
        from PIL import Image

        images = request.features.get("images")
        if not images or not isinstance(images, list):
            raise SchemaValidationError(
                _MODEL_NAME,
                ["Feature 'images' must be a list of numpy arrays."],
            )

        format_text = request.features.get("format", True)

        try:
            pil_images = [Image.fromarray(img).convert("RGB") for img in images]
            inputs = self._processor(
                pil_images,
                return_tensors="pt",
                multi_page=True,
                format=format_text,
            ).to(self._device)
            return self._generate_text(inputs)
        except Exception as exc:
            raise RuntimeError(
                f"GOT-OCR multi-page inference failed: {exc}"
            ) from exc

    def _predict_region(self, request: PredictionRequest) -> PredictionResult:
        """Run OCR on a specific region defined by box coordinates or color."""
        from PIL import Image

        image = self._extract_image(request, key="image")
        color = request.features.get("color")
        box = request.features.get("box")

        if not color and not box:
            raise SchemaValidationError(
                _MODEL_NAME,
                ["Must provide either 'color' or 'box' for region action."],
            )

        try:
            kwargs: dict[str, Any] = {"return_tensors": "pt"}
            if color:
                kwargs["color"] = color
            if box:
                kwargs["box"] = box

            pil_image = Image.fromarray(image).convert("RGB")
            inputs = self._processor(pil_image, **kwargs).to(self._device)
            return self._generate_text(inputs)
        except Exception as exc:
            raise RuntimeError(
                f"GOT-OCR region inference failed: {exc}"
            ) from exc

    def _generate_text(self, inputs: Any) -> PredictionResult:
        """Run text generation and decode the output."""
        try:
            import torch

            with torch.no_grad():
                generate_ids = self._model.generate(
                    **inputs,
                    do_sample=False,
                    tokenizer=self._processor.tokenizer,
                    stop_strings="<|im_end|>",
                    max_new_tokens=4096,
                )

            text = self._processor.decode(
                generate_ids[0, inputs["input_ids"].shape[1] :],
                skip_special_tokens=True,
            )

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={"text": text},
                confidence=1.0,
            )
        except Exception as exc:
            raise RuntimeError(f"GOT-OCR generation failed: {exc}") from exc

    def input_schema(self) -> dict[str, Any]:
        """Return the JSON schema for inputs."""
        return {
            "type": "object",
            # "action" n'est plus requis
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [a.value for a in GotOcrAction],
                },
                "image": {"type": "ndarray"},
                "images": {"type": "array"},
                "format": {"type": "boolean", "default": True},
                "box": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                },
                "color": {"type": "string"},
            },
        }

    def output_schema(self) -> dict[str, Any]:
        """Return the JSON schema for outputs."""
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
        }

    def _ensure_loaded(self) -> None:
        """Ensure the model and processor are initialized."""
        if self._model is None or self._processor is None:
            raise ModelLoadError(_MODEL_NAME, "Model is not loaded.")

    @staticmethod
    def _extract_action(request: PredictionRequest) -> GotOcrAction:
        """Extract or infer the requested action based on provided features."""
        raw_action = request.features.get("action")
        if raw_action is not None:
            try:
                return GotOcrAction(raw_action)
            except ValueError as exc:
                raise SchemaValidationError(
                    _MODEL_NAME, [f"Invalid action '{raw_action}'."]
                ) from exc

        features = request.features
        if "images" in features and isinstance(features["images"], list):
            return GotOcrAction.MULTI_PAGE
        if "box" in features or "color" in features:
            return GotOcrAction.REGION
        if "image" in features:
            return GotOcrAction.SINGLE_PAGE

        raise SchemaValidationError(
            _MODEL_NAME,
            [
                "Cannot infer action. Missing required features: 'image', 'images', 'box', or 'color'."
            ],
        )

    @staticmethod
    def _extract_image(
        request: PredictionRequest, *, key: str
    ) -> NDArray[np.uint8]:
        """Extract and validate a numpy image array."""
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
