"""GLM-OCR model wrapper for efficient local document OCR inference."""

from __future__ import annotations

import gc
import json
from collections.abc import Sequence
from enum import StrEnum, unique
from importlib.util import find_spec
from pathlib import Path
from threading import RLock
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

logger = structlog.get_logger(__name__)

_MODEL_NAME: Final = "glm_ocr"
_MODEL_VERSION: Final = "1.0.0"
_DEFAULT_ARTIFACT: Final = "zai-org/GLM-OCR"

_TASK_PROMPTS: Final[dict[GlmOcrTask, str]]


@unique
class GlmOcrAction(StrEnum):
    """Supported input modes."""

    SINGLE_PAGE = "single_page"
    MULTI_PAGE = "multi_page"
    REGION = "region"


@unique
class GlmOcrTask(StrEnum):
    """Tasks officially supported by GLM-OCR prompts."""

    TEXT = "text"
    FORMULA = "formula"
    TABLE = "table"
    INFORMATION_EXTRACTION = "information_extraction"


_TASK_PROMPTS = {
    GlmOcrTask.TEXT: "Text Recognition:",
    GlmOcrTask.FORMULA: "Formula Recognition:",
    GlmOcrTask.TABLE: "Table Recognition:",
}


@unique
class AttentionBackend(StrEnum):
    """Attention kernels accepted by Transformers for GLM-OCR."""

    AUTO = "auto"
    SDPA = "sdpa"
    EAGER = "eager"
    FLASH_ATTN_2 = "kernels-community/flash-attn2"


class GlmOcrModel(BaseModel):
    """GLM-OCR wrapper for text, formula, table, and JSON extraction."""

    def __init__(
        self,
        *,
        device: str = "auto",
        dtype: str = "auto",
        attention_backend: AttentionBackend | str = AttentionBackend.AUTO,
        revision: str | None = None,
        default_max_new_tokens: int = 4096,
        default_batch_size: int = 4,
    ) -> None:
        """Initialize the GLM-OCR integration without loading weights."""
        if default_max_new_tokens < 1:
            raise ValueError("default_max_new_tokens must be positive.")
        if default_batch_size < 1:
            raise ValueError("default_batch_size must be positive.")

        self._model: Any | None = None
        self._processor: Any | None = None
        self._device_preference = device
        self._dtype_preference = dtype
        self._attention_preference = AttentionBackend(attention_backend)
        self._revision = revision
        self._default_max_new_tokens = default_max_new_tokens
        self._default_batch_size = default_batch_size

        self._device: str = "cpu"
        self._input_device: Any | None = None
        self._dtype: Any | None = None
        self._inference_lock = RLock()

    def meta(self) -> ModelMeta:
        """Return the identity and capabilities of this integration."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description=(
                "GLM-OCR multimodal document OCR for text, formulas, tables, "
                "region crops, page batches, and schema-guided extraction."
            ),
            tags={
                "domain": "ocr",
                "backend": "transformers",
                "framework": "pytorch",
                "upstream": _DEFAULT_ARTIFACT,
                "architecture": "glm_ocr",
            },
        )

    def load(self, artifact_path: str = _DEFAULT_ARTIFACT) -> None:
        """Load GLM-OCR with a memory-conscious Transformers configuration."""
        try:
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "torch>=2.10 and transformers>=5.3 are required.",
            ) from exc

        with self._inference_lock:
            if self._model is not None or self._processor is not None:
                self.cleanup()

            try:
                self._device = self._resolve_device(torch)
                self._dtype = self._resolve_dtype(torch, self._device)
                attention = self._resolve_attention_backend(self._device)

                processor = AutoProcessor.from_pretrained(
                    artifact_path,
                    revision=self._revision,
                    local_files_only=False,
                )

                load_kwargs: dict[str, Any] = {
                    "pretrained_model_name_or_path": artifact_path,
                    "revision": self._revision,
                    "torch_dtype": self._dtype,
                    "low_cpu_mem_usage": True,
                    "attn_implementation": attention,
                }

                if self._device.startswith("cuda"):
                    load_kwargs["device_map"] = {"": self._device}

                model = AutoModelForImageTextToText.from_pretrained(
                    **load_kwargs
                )
                if not self._device.startswith("cuda"):
                    model.to(self._device)

                model.eval()
                model.requires_grad_(False)
                model.config.use_cache = True

                self._model = model
                self._processor = processor
                self._input_device = next(model.parameters()).device

                logger.info(
                    "model_loaded",
                    model_name=_MODEL_NAME,
                    artifact_path=artifact_path,
                    revision=self._revision,
                    device=str(self._input_device),
                    dtype=str(self._dtype),
                    attention_backend=attention,
                )
            except Exception as exc:
                self._model = None
                self._processor = None
                self._input_device = None
                raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str) -> None:
        """Download only the files required for local inference."""
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "huggingface_hub is not installed.",
            ) from exc

        try:
            target = Path(target_path)
            target.mkdir(parents=True, exist_ok=True)
            snapshot_download(
                repo_id=_DEFAULT_ARTIFACT,
                revision=self._revision,
                local_dir=target,
                allow_patterns=[
                    "*.json",
                    "*.jinja",
                    "*.safetensors",
                    "tokenizer.*",
                ],
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release model state and cached accelerator allocations."""
        with self._inference_lock:
            model = self._model
            processor = self._processor
            self._model = None
            self._processor = None
            self._input_device = None
            self._dtype = None

            del model, processor
            gc.collect()

            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                if hasattr(torch, "mps") and torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            except ImportError:
                pass

            logger.info("model_cleaned_up", model_name=_MODEL_NAME)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Infer the input mode and dispatch to the relevant OCR path."""
        self._ensure_loaded()
        action = self._extract_action(request)
        task = self._extract_task(request)
        prompt = self._resolve_prompt(request, task)
        max_new_tokens = self._extract_positive_int(
            request,
            key="max_new_tokens",
            default=self._default_max_new_tokens,
            maximum=8192,
        )

        match action:
            case GlmOcrAction.SINGLE_PAGE:
                image = self._extract_image(request, key="image")
                text = self._run_images(
                    [image],
                    prompt=prompt,
                    max_new_tokens=max_new_tokens,
                )[0]
                return self._result({"text": text, "task": task.value})

            case GlmOcrAction.REGION:
                image = self._extract_image(request, key="image")
                box = self._extract_box(request, image)
                x1, y1, x2, y2 = box
                # Crop first. Only the selected view can be copied later if
                # Pillow requires contiguous memory.
                region = image[y1:y2, x1:x2]
                text = self._run_images(
                    [region],
                    prompt=prompt,
                    max_new_tokens=max_new_tokens,
                )[0]
                return self._result(
                    {"text": text, "task": task.value, "box": list(box)}
                )

            case GlmOcrAction.MULTI_PAGE:
                images = self._extract_images(request)
                batch_size = self._extract_positive_int(
                    request,
                    key="batch_size",
                    default=self._default_batch_size,
                    maximum=32,
                )
                texts: list[str] = []
                for start in range(0, len(images), batch_size):
                    texts.extend(
                        self._run_images(
                            images[start : start + batch_size],
                            prompt=prompt,
                            max_new_tokens=max_new_tokens,
                        )
                    )
                return self._result({"texts": texts, "task": task.value})

        raise AssertionError(f"Unhandled action: {action}")

    def input_schema(self) -> dict[str, Any]:
        """Return the request feature schema."""
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [action.value for action in GlmOcrAction],
                },
                "task": {
                    "type": "string",
                    "enum": [task.value for task in GlmOcrTask],
                    "default": GlmOcrTask.TEXT.value,
                },
                "image": {"type": "ndarray"},
                "images": {
                    "type": "array",
                    "items": {"type": "ndarray"},
                    "minItems": 1,
                },
                "box": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "Region [x1, y1, x2, y2], x2/y2 exclusive.",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Required only for information_extraction unless "
                        "json_schema is supplied."
                    ),
                },
                "json_schema": {
                    "type": "object",
                    "description": "Target JSON shape for information extraction.",
                },
                "max_new_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8192,
                    "default": self._default_max_new_tokens,
                },
                "batch_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 32,
                    "default": self._default_batch_size,
                },
            },
        }

    def output_schema(self) -> dict[str, Any]:
        """Return the prediction payload schema."""
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string"},
                "texts": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "task": {
                    "type": "string",
                    "enum": [task.value for task in GlmOcrTask],
                },
                "box": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                },
            },
        }

    def _run_images(
        self,
        images: Sequence[NDArray[np.uint8]],
        *,
        prompt: str,
        max_new_tokens: int,
    ) -> list[str]:
        """Preprocess and greedily decode one bounded image batch."""
        from PIL import Image

        del Image
        self._ensure_loaded()
        assert self._processor is not None
        assert self._model is not None
        assert self._input_device is not None

        pil_images = [self._to_pil_rgb(image) for image in images]
        conversation = [
            {
                "role": "user",
                "content": [
                    {"type": "image"},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        rendered_prompt = self._processor.apply_chat_template(
            conversation,
            tokenize=False,
            add_generation_prompt=True,
        )
        rendered_prompts = [rendered_prompt] * len(pil_images)

        try:
            import torch

            with self._inference_lock, torch.inference_mode():
                inputs = self._processor(
                    text=rendered_prompts,
                    images=pil_images,
                    padding=True,
                    return_tensors="pt",
                )
                inputs.pop("token_type_ids", None)
                inputs = inputs.to(self._input_device)

                prompt_width = int(inputs["input_ids"].shape[1])
                generated_ids = self._model.generate(
                    **inputs,
                    do_sample=False,
                    use_cache=True,
                    max_new_tokens=max_new_tokens,
                    eos_token_id=self._model.generation_config.eos_token_id,
                    pad_token_id=self._model.generation_config.pad_token_id,
                )
                new_tokens = generated_ids[:, prompt_width:]
                texts = self._processor.batch_decode(
                    new_tokens,
                    skip_special_tokens=True,
                    clean_up_tokenization_spaces=False,
                )

            return [text.strip() for text in texts]
        except Exception as exc:
            raise RuntimeError(f"GLM-OCR inference failed: {exc}") from exc
        finally:
            # Close Pillow objects deterministically. Do not call empty_cache()
            # here: that would force expensive allocator churn on every request.
            for image in pil_images:
                image.close()

    def _resolve_device(self, torch: Any) -> str:
        """Resolve the requested accelerator."""
        requested = self._device_preference.lower()
        if requested != "auto":
            if requested.startswith("cuda") and not torch.cuda.is_available():
                raise RuntimeError("CUDA was requested but is unavailable.")
            if requested == "mps" and not torch.backends.mps.is_available():
                raise RuntimeError("MPS was requested but is unavailable.")
            return requested

        if torch.cuda.is_available():
            return "cuda:0"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def _resolve_dtype(self, torch: Any, device: str) -> Any:
        """Choose a stable low-memory dtype for the selected device."""
        requested = self._dtype_preference.lower()
        if requested != "auto":
            aliases = {
                "float32": torch.float32,
                "fp32": torch.float32,
                "float16": torch.float16,
                "fp16": torch.float16,
                "bfloat16": torch.bfloat16,
                "bf16": torch.bfloat16,
            }
            try:
                return aliases[requested]
            except KeyError as exc:
                raise ValueError(f"Unsupported dtype: {requested}") from exc

        if device.startswith("cuda"):
            return (
                torch.bfloat16
                if torch.cuda.is_bf16_supported()
                else torch.float16
            )
        if device == "mps":
            return torch.float16
        return torch.float32

    def _resolve_attention_backend(self, device: str) -> str:
        """Select a robust attention implementation."""
        requested = self._attention_preference
        if requested is AttentionBackend.AUTO:
            return AttentionBackend.SDPA.value

        if requested is AttentionBackend.FLASH_ATTN_2:
            if not device.startswith("cuda"):
                raise RuntimeError("FlashAttention 2 requires CUDA.")
            if find_spec("flash_attn") is None:
                logger.warning(
                    "flash_attn_python_package_not_found",
                    note=(
                        "Transformers kernel loading may still work, but the "
                        "deployment should be validated before production."
                    ),
                )
        return requested.value

    def _resolve_prompt(
        self,
        request: PredictionRequest,
        task: GlmOcrTask,
    ) -> str:
        """Return one of the official prompts or a strict JSON extraction prompt."""
        if task is not GlmOcrTask.INFORMATION_EXTRACTION:
            return _TASK_PROMPTS[task]

        custom_prompt = request.features.get("prompt")
        if custom_prompt is not None:
            if not isinstance(custom_prompt, str) or not custom_prompt.strip():
                raise SchemaValidationError(
                    _MODEL_NAME,
                    ["Feature 'prompt' must be a non-empty string."],
                )
            return custom_prompt.strip()

        schema = request.features.get("json_schema")
        if not isinstance(schema, dict) or not schema:
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    "Task 'information_extraction' requires either a non-empty "
                    "'prompt' or a non-empty 'json_schema'."
                ],
            )

        compact_schema = json.dumps(
            schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            "Extract the information from the image and return only valid JSON "
            "matching exactly this schema:\n"
            f"{compact_schema}"
        )

    def _ensure_loaded(self) -> None:
        """Ensure model, processor, and target device are initialized."""
        if (
            self._model is None
            or self._processor is None
            or self._input_device is None
        ):
            raise ModelLoadError(_MODEL_NAME, "Model is not loaded.")

    @staticmethod
    def _result(prediction: dict[str, Any]) -> PredictionResult:
        """Build a result without computing expensive token-level scores."""
        return PredictionResult(
            model_name=_MODEL_NAME,
            model_version=_MODEL_VERSION,
            prediction=prediction,
            # GLM-OCR does not expose a calibrated sequence confidence. Keep
            # this compatibility value only if PredictionResult requires float.
            confidence=1.0,
        )

    @staticmethod
    def _extract_action(request: PredictionRequest) -> GlmOcrAction:
        """Extract or infer the input mode from request features."""
        raw_action = request.features.get("action")
        if raw_action is not None:
            try:
                return GlmOcrAction(raw_action)
            except ValueError as exc:
                raise SchemaValidationError(
                    _MODEL_NAME,
                    [f"Invalid action '{raw_action}'."],
                ) from exc

        features = request.features
        if isinstance(features.get("images"), list):
            return GlmOcrAction.MULTI_PAGE
        if "box" in features:
            return GlmOcrAction.REGION
        if "image" in features:
            return GlmOcrAction.SINGLE_PAGE

        raise SchemaValidationError(
            _MODEL_NAME,
            ["Cannot infer action: provide 'image', 'images', or 'box'."],
        )

    @staticmethod
    def _extract_task(request: PredictionRequest) -> GlmOcrTask:
        """Extract the requested OCR task, defaulting to text recognition."""
        raw_task = request.features.get("task", GlmOcrTask.TEXT.value)
        try:
            return GlmOcrTask(raw_task)
        except ValueError as exc:
            raise SchemaValidationError(
                _MODEL_NAME,
                [f"Invalid task '{raw_task}'."],
            ) from exc

    @classmethod
    def _extract_image(
        cls,
        request: PredictionRequest,
        *,
        key: str,
    ) -> NDArray[np.uint8]:
        """Extract and validate one uint8 image array."""
        image = request.features.get(key)
        if image is None:
            raise SchemaValidationError(
                _MODEL_NAME,
                [f"Missing required feature: '{key}'."],
            )
        return cls._validate_image(image, key=key)

    @classmethod
    def _extract_images(
        cls,
        request: PredictionRequest,
    ) -> list[NDArray[np.uint8]]:
        """Extract a non-empty list of independently processed pages."""
        images = request.features.get("images")
        if not isinstance(images, list) or not images:
            raise SchemaValidationError(
                _MODEL_NAME,
                ["Feature 'images' must be a non-empty list of numpy arrays."],
            )
        return [
            cls._validate_image(image, key=f"images[{index}]")
            for index, image in enumerate(images)
        ]

    @staticmethod
    def _validate_image(value: Any, *, key: str) -> NDArray[np.uint8]:
        """Validate shape and dtype without copying the image."""
        if not isinstance(value, np.ndarray):
            raise SchemaValidationError(
                _MODEL_NAME,
                [f"Feature '{key}' must be a numpy array."],
            )
        if value.dtype != np.uint8:
            raise SchemaValidationError(
                _MODEL_NAME,
                [f"Feature '{key}' must have dtype uint8."],
            )
        if value.ndim not in (2, 3):
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Feature '{key}' must have shape HxW, HxWx1, HxWx3, or HxWx4."
                ],
            )
        if value.shape[0] < 1 or value.shape[1] < 1:
            raise SchemaValidationError(
                _MODEL_NAME,
                [f"Feature '{key}' must not be empty."],
            )
        if value.ndim == 3 and value.shape[2] not in (1, 3, 4):
            raise SchemaValidationError(
                _MODEL_NAME,
                [f"Feature '{key}' has an unsupported channel count."],
            )
        return value

    @staticmethod
    def _extract_box(
        request: PredictionRequest,
        image: NDArray[np.uint8],
    ) -> tuple[int, int, int, int]:
        """Validate an exclusive-end rectangle against image bounds."""
        box = request.features.get("box")
        if (
            not isinstance(box, (list, tuple))
            or len(box) != 4
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in box
            )
        ):
            raise SchemaValidationError(
                _MODEL_NAME,
                ["Feature 'box' must be four integers [x1, y1, x2, y2]."],
            )

        x1, y1, x2, y2 = box
        height, width = image.shape[:2]
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    "Feature 'box' is outside image bounds or has non-positive "
                    f"area: box={box}, image_size=({width}, {height})."
                ],
            )
        return x1, y1, x2, y2

    @staticmethod
    def _extract_positive_int(
        request: PredictionRequest,
        *,
        key: str,
        default: int,
        maximum: int,
    ) -> int:
        """Extract a bounded positive integer feature."""
        value = request.features.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Feature '{key}' must be an integer between 1 and {maximum}."
                ],
            )
        return value

    @staticmethod
    def _to_pil_rgb(image: NDArray[np.uint8]) -> Any:
        """Create an RGB Pillow image with at most one necessary array copy."""
        from PIL import Image

        array = image
        if array.ndim == 3 and array.shape[2] == 1:
            array = array[:, :, 0]
        if not array.flags.c_contiguous:
            array = np.ascontiguousarray(array)

        pil_image = Image.fromarray(array)
        if pil_image.mode == "RGB":
            return pil_image

        rgb_image = pil_image.convert("RGB")
        pil_image.close()
        return rgb_image
