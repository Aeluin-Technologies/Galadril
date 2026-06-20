from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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

_MODEL_NAME = "qwen_embedding"
_MODEL_VERSION = "3.0.0"


class Qwen3EmbeddingModel(BaseModel):
    """Heterogeneous embedding model supporting Qwen3 text and vision GGUF variants."""

    def __init__(self) -> None:
        self._ctx: Any | None = None
        self._model_tier: str = "0.6b"
        self._compute_type: str = "q6_k"
        self._max_dims: dict[str, int] = {
            "0.6b": 1024,
            "4b": 2560,
            "8b": 4096,
        }

    def meta(self) -> ModelMeta:
        """Return the immutable identity of this model."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Qwen3 Embedding supporting native multi-vector / multimodal retrieval and MRL.",
            tags={
                "domain": "multimodal" if "8b" in self._model_tier else "nlp",
                "backend": "llama.cpp",
                "framework": "gguf",
            },
        )

    def load(
        self,
        artifact_path: str,
        model_tier: str = "0.6b",
        compute_type: str = "q6_k",
        n_gpu_layers: int = -1,
        n_ctx: int = 32768,
    ) -> None:
        """Load the Qwen3 GGUF embedding model from a single unified file.

        Args:
            artifact_path: Local directory to store/load the weights.
            model_tier: Choose from '0.6b', '4b', or '8b'.
            compute_type: Choose quantization 'q6_k' (recommended) or 'q8_0'.
            n_gpu_layers: Number of layers to offload to GPU (-1 for all).
            n_ctx: Context window size.
        """
        try:
            from llama_cpp import Llama
            from huggingface_hub import hf_hub_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "llama-cpp-python or huggingface_hub is not installed.",
            ) from exc

        if (
            self._ctx is not None
            and self._model_tier == model_tier
            and self._compute_type == compute_type
        ):
            return

        self._model_tier = model_tier.lower()
        self._compute_type = compute_type.lower()
        os.makedirs(artifact_path, exist_ok=True)

        repo_map = {
            "0.6b": "batiai/Qwen3-Embedding-0.6B-GGUF",
            "4b": "batiai/Qwen3-Embedding-4B-GGUF",
            "8b": "batiai/Qwen3-VL-Embedding-8B-GGUF",
        }

        if self._model_tier not in repo_map:
            raise ModelLoadError(
                _MODEL_NAME, f"Unsupported model tier: {model_tier}"
            )

        repo_id = repo_map[self._model_tier]
        quant_suffix = (
            "Q6_K" if self._compute_type in ["q6_k", "int6"] else "Q8_0"
        )

        if self._model_tier == "0.6b":
            model_file = f"Qwen3-Embedding-0.6B-{quant_suffix}.gguf"
        elif self._model_tier == "4b":
            model_file = f"Qwen3-Embedding-4B-{quant_suffix}.gguf"
        else:
            model_file = f"Qwen3-VL-Embedding-8B-{quant_suffix}.gguf"

        try:
            model_path = hf_hub_download(
                repo_id=repo_id,
                filename=model_file,
                local_dir=artifact_path,
            )

            self._ctx = Llama(
                model_path=model_path,
                embedding=True,
                n_ctx=n_ctx,
                n_gpu_layers=n_gpu_layers,
                pooling_type=3,
                verbose=False,
            )
        except Exception as exc:
            self.cleanup()
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(
        self,
        target_path: str,
        model_tier: str = "0.6b",
        compute_type: str = "q6_k",
    ) -> None:
        """Download the selected Qwen3 GGUF checkpoint into target_path."""
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "huggingface_hub is not installed.",
            ) from exc

        model_tier = model_tier.lower()
        compute_type = compute_type.lower()

        repo_map = {
            "0.6b": "batiai/Qwen3-Embedding-0.6B-GGUF",
            "4b": "batiai/Qwen3-Embedding-4B-GGUF",
            "8b": "batiai/Qwen3-VL-Embedding-8B-GGUF",
        }
        if model_tier not in repo_map:
            raise ModelLoadError(
                _MODEL_NAME, f"Unsupported model tier: {model_tier}"
            )

        quant_suffix = "Q6_K" if compute_type in ["q6_k", "int6"] else "Q8_0"
        if model_tier == "0.6b":
            model_file = f"Qwen3-Embedding-0.6B-{quant_suffix}.gguf"
        elif model_tier == "4b":
            model_file = f"Qwen3-Embedding-4B-{quant_suffix}.gguf"
        else:
            model_file = f"Qwen3-VL-Embedding-8B-{quant_suffix}.gguf"

        try:
            Path(target_path).mkdir(parents=True, exist_ok=True)
            hf_hub_download(
                repo_id=repo_map[model_tier],
                filename=model_file,
                local_dir=target_path,
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release native C++ resources and clear context."""
        if self._ctx is not None:
            del self._ctx
            self._ctx = None

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run embedding inference with support for Instructions and MRL."""
        self._ensure_loaded()

        features = request.features
        text = features.get("text", "")
        task = features.get("task", None)

        max_allowed = self._max_dims.get(self._model_tier, 1024)
        target_dim = features.get("dimensions", max_allowed)
        if target_dim > max_allowed:
            target_dim = max_allowed

        if task and isinstance(task, str) and text:
            processed_input = f"Instruct: {task}\nQuery: {text}"
        else:
            processed_input = text

        try:
            if not processed_input:
                raise SchemaValidationError(
                    _MODEL_NAME, ["A non-empty 'text' feature is required."]
                )

            raw_response = self._ctx.create_embedding(input=processed_input)
            raw_embedding = raw_response["data"][0]["embedding"]

            actual_model_dim = len(raw_embedding)
            final_dim = min(target_dim, actual_model_dim)

            sliced_emb = np.array(raw_embedding[:final_dim], dtype=np.float32)
            norm = np.linalg.norm(sliced_emb)
            if norm > 1e-9:
                sliced_emb = sliced_emb / norm

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={
                    "embedding": sliced_emb.tolist(),
                    "dimension": final_dim,
                },
                confidence=1.0,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Qwen3 GGUF embedding inference failed: {exc}"
            ) from exc

    def input_schema(self) -> dict[str, Any]:
        """Return a JSON Schema dict describing expected input features."""
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "task": {"type": "string"},
                "dimensions": {"type": "integer"},
            },
            "required": ["text"],
        }

    def output_schema(self) -> dict[str, Any]:
        """Return a JSON Schema dict describing the prediction output."""
        return {
            "type": "object",
            "properties": {
                "embedding": {"type": "array", "items": {"type": "number"}},
                "dimension": {"type": "integer"},
            },
            "required": ["embedding", "dimension"],
        }

    def _ensure_loaded(self) -> None:
        if self._ctx is None:
            raise ModelLoadError(
                _MODEL_NAME,
                "Model context is uninitialized.",
            )
