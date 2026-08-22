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
from galadril_inference.models.runtime import create_session

logger = structlog.get_logger(__name__)

_MODEL_NAME = "siglip2"
_MODEL_VERSION = "2.1.0"
_ONNX_REPO = "onnx-community/siglip2-base-patch16-384-ONNX"


@unique
class SigLIPAction(StrEnum):
    """Supported inference actions for the SigLIP model."""

    EMBED_IMAGE = "embed_image"
    EMBED_TEXT = "embed_text"


class SigLIPModel(BaseModel):
    """Google SigLIP 2 model for multimodal feature extraction."""

    def __init__(self) -> None:
        self._image_session: Any | None = None
        self._text_session: Any | None = None
        self._processor: Any | None = None

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Extracts normalized image and text embeddings using quantized SigLIP2 ONNX encoders.",
            tags={
                "domain": "multimodal",
                "backend": "onnxruntime",
                "framework": "onnx",
            },
        )

    def load(
        self,
        artifact_path: str,
        compute_type: str = "int8",
        device: str = "auto",
    ) -> None:
        """Load lightweight SigLIP2 ONNX image and text encoders."""
        try:
            from transformers import AutoProcessor
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "transformers is required for SigLIP preprocessing.",
            ) from exc

        try:
            root = Path(artifact_path)
            suffix = self._compute_suffix(compute_type)
            image_path = root / "onnx" / f"vision_model{suffix}.onnx"
            text_path = root / "onnx" / f"text_model{suffix}.onnx"
            if not image_path.is_file() or not text_path.is_file():
                raise FileNotFoundError(
                    "SigLIP2 ONNX artifacts are missing. Call download() with "
                    f"compute_type='{compute_type}' before load()."
                )

            self._processor = AutoProcessor.from_pretrained(
                str(root), local_files_only=True
            )
            self._image_session = create_session(image_path, device=device)
            self._text_session = create_session(text_path, device=device)

            logger.info(
                "model_loaded",
                model_name=_MODEL_NAME,
                compute_type=compute_type,
                image_providers=self._image_session.get_providers(),
                text_providers=self._text_session.get_providers(),
            )
        except Exception as exc:
            self.cleanup()
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str, compute_type: str = "int8") -> None:
        """Download only the selected SigLIP2 ONNX encoders and processors."""
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
                    "tokenizer.model",
                    f"onnx/text_model{suffix}.onnx",
                    f"onnx/vision_model{suffix}.onnx",
                ],
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release the model from memory."""
        self._image_session = None
        self._text_session = None
        self._processor = None
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
        image = self._extract_image(request, key="image")

        try:
            inputs = self._processor(images=[image], return_tensors="np")
            image_features = self._run_encoder(
                self._image_session,
                inputs,
                preferred_outputs=("image_embeds", "pooler_output"),
            )
            embedding_list = self._normalize(image_features).tolist()

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
        text = request.features.get("text")
        if not text or not isinstance(text, str):
            raise SchemaValidationError(
                _MODEL_NAME, ["Feature 'text' must be a non-empty string."]
            )

        try:
            inputs = self._processor(
                text=[text], padding="max_length", return_tensors="np"
            )
            text_features = self._run_encoder(
                self._text_session,
                inputs,
                preferred_outputs=("text_embeds", "pooler_output"),
            )
            embedding_list = self._normalize(text_features).tolist()

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
        if (
            self._image_session is None
            or self._text_session is None
            or self._processor is None
        ):
            raise ModelLoadError(_MODEL_NAME, "Model is not loaded.")

    @staticmethod
    def _compute_suffix(compute_type: str) -> str:
        """Map a public precision name to the ONNX Community artifact suffix."""
        normalized = compute_type.lower()
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
            return suffixes[normalized]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported SigLIP compute type '{compute_type}'."
            ) from exc

    @staticmethod
    def _run_encoder(
        session: Any,
        values: dict[str, Any],
        *,
        preferred_outputs: tuple[str, ...],
    ) -> NDArray[np.float32]:
        """Run an encoder with contiguous inputs and select its pooled output."""
        input_names = {value.name for value in session.get_inputs()}
        feeds = {
            key: np.ascontiguousarray(value)
            for key, value in values.items()
            if key in input_names
        }
        output_meta = session.get_outputs()
        outputs = session.run(None, feeds)
        by_name = {
            meta.name: value
            for meta, value in zip(output_meta, outputs, strict=True)
        }
        selected = next(
            (by_name[name] for name in preferred_outputs if name in by_name),
            outputs[-1],
        )
        embedding = np.asarray(selected, dtype=np.float32)
        if embedding.ndim == 2 and embedding.shape[0] == 1:
            return embedding[0]
        if embedding.ndim == 1:
            return embedding
        raise RuntimeError(
            f"Unexpected SigLIP embedding output shape: {embedding.shape}."
        )

    @staticmethod
    def _normalize(embedding: NDArray[np.float32]) -> NDArray[np.float32]:
        """Normalize an embedding in place when its norm is non-zero."""
        norm = float(np.linalg.norm(embedding))
        if norm > 1e-12:
            embedding /= norm
        return embedding

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
