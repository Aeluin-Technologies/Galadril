"""BGE-M3 model for dense, sparse, and ColBERT embeddings via ONNX."""

from __future__ import annotations

import os
from collections import defaultdict
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

_MODEL_NAME = "bge_m3"
_MODEL_VERSION = "1.1.0"


class BgeM3Model(BaseModel):
    """BGE-M3 embedding model supporting dense, sparse and ColBERT."""

    def __init__(self) -> None:
        self._session: Any | None = None
        self._tokenizer: Any | None = None

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="BGE-M3 supporting dense retrieval, lexical matching and multi-vector interaction (ColBERT).",
            tags={
                "domain": "nlp",
                "backend": "onnxruntime",
                "framework": "onnx",
            },
            deprecated=True,
        )

    def load(self, artifact_path: str, compute_type: str = "default") -> None:
        """Load the BGE-M3 tokenizer and ONNX model with optional quantization."""
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "onnxruntime, transformers or huggingface_hub is not installed.",
            ) from exc

        try:
            artifact_root = Path(artifact_path)
            tokenizer_dir = artifact_root / "tokenizer"
            artifact_root / "onnx"
            tokenizer_source = (
                str(tokenizer_dir)
                if tokenizer_dir.is_dir() and any(tokenizer_dir.iterdir())
                else "BAAI/bge-m3"
            )
            self._tokenizer = AutoTokenizer.from_pretrained(tokenizer_source)

            os.makedirs(artifact_path, exist_ok=True)
            repo_id = "Xenova/bge-m3"

            if compute_type in ["float16", "fp16"]:
                target_files = ["onnx/model_fp16.onnx"]
            elif compute_type == "int8":
                target_files = ["onnx/model_int8.onnx"]
            elif compute_type in ["int4", "q4"]:
                target_files = ["onnx/model_q4.onnx"]
            else:
                target_files = ["onnx/model.onnx", "onnx/model.onnx_data"]

            model_path = ""
            for file_name in target_files:
                local_candidate = artifact_root / file_name
                if local_candidate.exists():
                    downloaded_path = str(local_candidate)
                else:
                    downloaded_path = hf_hub_download(
                        repo_id=repo_id,
                        filename=file_name,
                        local_dir=artifact_path,
                    )
                if file_name.endswith(".onnx"):
                    model_path = downloaded_path

            if not model_path:
                raise FileNotFoundError(
                    f"Failed to resolve ONNX model path for {compute_type}."
                )

            self._session = ort.InferenceSession(
                model_path,
                providers=[
                    "CUDAExecutionProvider",
                    "CPUExecutionProvider",
                ],
            )

            logger.info(
                "model_loaded",
                model_name=_MODEL_NAME,
                path=model_path,
                compute_type=compute_type,
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str, compute_type: str = "default") -> None:
        """Download the BGE-M3 tokenizer and ONNX export into target_path."""
        try:
            from huggingface_hub import hf_hub_download, snapshot_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "huggingface_hub is not installed.",
            ) from exc

        try:
            artifact_root = Path(target_path)
            tokenizer_dir = artifact_root / "tokenizer"
            onnx_dir = artifact_root / "onnx"
            tokenizer_dir.mkdir(parents=True, exist_ok=True)
            onnx_dir.mkdir(parents=True, exist_ok=True)

            snapshot_download(
                repo_id="BAAI/bge-m3",
                local_dir=str(tokenizer_dir),
            )

            if compute_type in ["float16", "fp16"]:
                target_files = ["onnx/model_fp16.onnx"]
            elif compute_type == "int8":
                target_files = ["onnx/model_int8.onnx"]
            elif compute_type in ["int4", "q4"]:
                target_files = ["onnx/model_q4.onnx"]
            else:
                target_files = ["onnx/model.onnx", "onnx/model.onnx_data"]

            for file_name in target_files:
                hf_hub_download(
                    repo_id="Xenova/bge-m3",
                    filename=file_name,
                    local_dir=target_path,
                )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release the model from memory."""
        self._session = None
        self._tokenizer = None
        logger.info("model_cleaned_up", model_name=_MODEL_NAME)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run embedding inference."""
        self._ensure_loaded()

        text = request.features.get("text")
        if not text or not isinstance(text, str):
            raise SchemaValidationError(
                _MODEL_NAME, ["Feature 'text' must be a non-empty string."]
            )

        try:
            import onnxruntime as ort

            inputs = self._tokenizer(
                text, padding="longest", return_tensors="np"
            )
            inputs_onnx = {
                k: ort.OrtValue.ortvalue_from_numpy(v)
                for k, v in inputs.items()
            }

            outputs = self._session.run(None, inputs_onnx)

            if len(outputs) >= 3:
                dense_vecs = outputs[0][0].tolist()

                token_weights = outputs[1].squeeze(-1)
                sparse_dict = self._process_token_weights(
                    token_weights[0], inputs["input_ids"][0].tolist()
                )

                colbert_vecs = outputs[2][0].tolist()
            else:
                last_hidden_state = outputs[0]

                dense_np = last_hidden_state[0, 0, :]
                dense_np = dense_np / (np.linalg.norm(dense_np) + 1e-9)
                dense_vecs = dense_np.tolist()

                colbert_np = last_hidden_state[0]
                norms = (
                    np.linalg.norm(colbert_np, axis=-1, keepdims=True) + 1e-9
                )
                colbert_np = colbert_np / norms
                colbert_vecs = colbert_np.tolist()

                sparse_dict = {}

            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={
                    "dense": dense_vecs,
                    "sparse": sparse_dict,
                    "colbert": colbert_vecs,
                },
                confidence=1.0,
            )

        except Exception as exc:
            raise RuntimeError(f"BGE-M3 ONNX inference failed: {exc}") from exc

    def _process_token_weights(
        self, token_weights: np.ndarray, input_ids: list
    ):
        """Convert token weights to a dictionary of lexical weights."""
        result = defaultdict(float)
        unused_tokens = {
            self._tokenizer.cls_token_id,
            self._tokenizer.eos_token_id,
            self._tokenizer.pad_token_id,
            self._tokenizer.unk_token_id,
        }
        for w, idx in zip(token_weights, input_ids, strict=False):
            if idx not in unused_tokens and w > 0:
                idx_str = str(idx)
                if w > result[idx_str]:
                    result[idx_str] = float(w)
        return dict(result)

    @staticmethod
    def colbert_score(q_vec: np.ndarray, d_vec: np.ndarray) -> float:
        """Calculate ColBERT max-sim score between query and document."""
        return float(np.sum(np.max(np.dot(q_vec, d_vec.T), axis=1)))

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "description": "Text to embed."},
            },
        }

    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dense": {"type": "array", "items": {"type": "number"}},
                "sparse": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
                "colbert": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                },
            },
        }

    def _ensure_loaded(self) -> None:
        if self._session is None or self._tokenizer is None:
            raise ModelLoadError(
                _MODEL_NAME,
                "Model is not loaded. Call load() before predict().",
            )
