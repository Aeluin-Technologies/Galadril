"""Text retrieval profiles explicitly separate trained last-token and late-mean pooling."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Protocol, cast

import numpy as np
import structlog
from pydantic import BaseModel as Payload
from pydantic import (
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
)

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
_MODEL_VERSION = "3.1.0"
_DIMENSIONS = {"0.6b": 1024, "4b": 2560, "8b": 4096}
_TIERS = {"0.6b": "0.6B", "4b": "4B", "8b": "8B"}
_REVISIONS = {
    "batiai/Qwen3-Embedding-0.6B-GGUF": "d84b43e69be2a7f360b062b454f313783d8a8e36",
    "batiai/Qwen3-Embedding-4B-GGUF": "e2cc8e510afaa9120246351d7d2956722d270ff7",
    "Qwen/Qwen3-Embedding-0.6B-GGUF": "370f27d7550e0def9b39c1f16d3fbaa13aa67728",
    "Qwen/Qwen3-Embedding-4B-GGUF": "f4602530db1d980e16da9d7d3a70294cf5c190be",
    "Qwen/Qwen3-Embedding-8B-GGUF": "69d0e58a13e463cd99a9b83e3f5fee7c10265fab",
}


class EmbeddingContext(Protocol):
    def tokenize(
        self, text: bytes, *, add_bos: bool = True, special: bool = False
    ) -> list[int]: ...
    def embed(
        self, text: str, *, normalize: bool = False, truncate: bool = True
    ) -> object: ...
    def close(self) -> None: ...


class Features(Payload):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: StrictStr = Field(min_length=1, max_length=131072)
    task: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    dimensions: StrictInt | None = Field(default=None, ge=1, le=4096)
    token_spans: tuple[tuple[StrictInt, StrictInt], ...] = Field(
        default=(), max_length=128
    )


class Qwen3EmbeddingModel(BaseModel):
    """The default profile retains Qwen's trained last-token retrieval contract."""

    __slots__ = (
        "_ctx",
        "_model_tier",
        "_compute_type",
        "_embedding_mode",
        "_context_tokens",
        "_load_signature",
        "_weights_sha256",
        "_lock",
    )

    def __init__(self) -> None:
        self._ctx: EmbeddingContext | None = None
        self._model_tier = "0.6b"
        self._compute_type = "q6_k"
        self._embedding_mode = "pooled"
        self._context_tokens = 32768
        self._load_signature: tuple[str, str, str, int, int, str] | None = None
        self._weights_sha256 = "unloaded"
        self._lock = threading.RLock()

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Qwen3 text embeddings with explicit pooled or contextual-token profiles.",
            tags={
                "domain": "nlp",
                "backend": "llama.cpp",
                "framework": "gguf",
                "pooling": self._embedding_mode,
            },
        )

    @staticmethod
    def artifact(model_tier: str, compute_type: str) -> tuple[str, str]:
        tier = _TIERS.get(model_tier.lower())
        if tier is None or compute_type.lower() not in {"q6_k", "int6", "q8_0"}:
            raise ModelLoadError(
                _MODEL_NAME, "Unsupported embedding tier or quantization"
            )
        suffix = "Q6_K" if compute_type.lower() in {"q6_k", "int6"} else "Q8_0"
        owner = (
            "batiai"
            if model_tier.lower() in {"0.6b", "4b"} and suffix == "Q6_K"
            else "Qwen"
        )
        return (
            f"{owner}/Qwen3-Embedding-{tier}-GGUF",
            f"Qwen3-Embedding-{tier}-{suffix}.gguf",
        )

    def load(
        self,
        artifact_path: str,
        model_tier: str = "0.6b",
        compute_type: str = "q6_k",
        n_gpu_layers: int = -1,
        n_ctx: int = 32768,
        embedding_mode: str = "pooled",
    ) -> None:
        if (
            embedding_mode not in {"pooled", "tokens"}
            or not 1 <= n_ctx <= 32768
        ):
            raise ModelLoadError(
                _MODEL_NAME, "Invalid embedding context or pooling mode"
            )
        repo, filename = self.artifact(model_tier, compute_type)
        signature = (
            str(Path(artifact_path).resolve()),
            model_tier.lower(),
            compute_type.lower(),
            n_gpu_layers,
            n_ctx,
            embedding_mode,
        )
        with self._lock:
            if self._ctx is not None and self._load_signature == signature:
                return
            try:
                from huggingface_hub import hf_hub_download
                from llama_cpp import Llama

                Path(artifact_path).mkdir(parents=True, exist_ok=True)
                model_path = hf_hub_download(
                    repo_id=repo,
                    filename=filename,
                    local_dir=artifact_path,
                    revision=_REVISIONS[repo],
                )
                with Path(model_path).open("rb") as source:
                    checksum = hashlib.file_digest(source, "sha256").hexdigest()
                context = cast(
                    EmbeddingContext,
                    Llama(
                        model_path=model_path,
                        embedding=True,
                        n_ctx=n_ctx,
                        n_batch=n_ctx,
                        n_ubatch=n_ctx
                        if embedding_mode == "pooled"
                        else min(512, n_ctx),
                        n_gpu_layers=n_gpu_layers,
                        pooling_type=3 if embedding_mode == "pooled" else 0,
                        verbose=False,
                    ),
                )
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                logger.error(
                    "embedding.load.failed", error_type=type(error).__name__
                )
                raise ModelLoadError(
                    _MODEL_NAME, "Embedding runtime unavailable"
                ) from None
            self.cleanup()
            self._ctx = context
            self._model_tier = model_tier.lower()
            self._compute_type = compute_type.lower()
            self._embedding_mode = embedding_mode
            self._context_tokens = n_ctx
            self._weights_sha256 = checksum
            self._load_signature = signature
            logger.info(
                "embedding.loaded",
                tier=self._model_tier,
                pooling=embedding_mode,
                context_tokens=n_ctx,
            )

    def download(
        self,
        target_path: str,
        model_tier: str = "0.6b",
        compute_type: str = "q6_k",
    ) -> None:
        repo, filename = self.artifact(model_tier, compute_type)
        try:
            from huggingface_hub import hf_hub_download

            Path(target_path).mkdir(parents=True, exist_ok=True)
            hf_hub_download(
                repo_id=repo,
                filename=filename,
                local_dir=target_path,
                revision=_REVISIONS[repo],
            )
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            logger.error(
                "embedding.download.failed", error_type=type(error).__name__
            )
            raise ModelLoadError(
                _MODEL_NAME, "Embedding artifact unavailable"
            ) from None

    def cleanup(self) -> None:
        with self._lock:
            if self._ctx is not None:
                self._ctx.close()
                self._ctx = None
            self._load_signature = None

    def predict(self, request: PredictionRequest) -> PredictionResult:
        try:
            features = Features.model_validate(request.features)
        except ValidationError:
            raise SchemaValidationError(
                _MODEL_NAME, ["Invalid text retrieval features"]
            ) from None
        dimensions = features.dimensions or _DIMENSIONS[self._model_tier]
        if (
            dimensions > _DIMENSIONS[self._model_tier]
            or not features.text.strip()
            or len(features.text.encode()) > 131072
        ):
            raise SchemaValidationError(
                _MODEL_NAME, ["Invalid text or output dimensions"]
            )
        if features.token_spans and (
            self._embedding_mode != "tokens" or features.task is not None
        ):
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    "Token spans require an unprefixed contextual-token document profile"
                ],
            )
        text = (
            f"Instruct: {features.task}\nQuery: {features.text}"
            if features.task
            else features.text
        )
        with self._lock:
            context = self._ctx
            if context is None:
                raise ModelLoadError(
                    _MODEL_NAME, "Embedding context is uninitialized"
                )
            tokens = context.tokenize(text.encode())
            if len(tokens) > self._context_tokens or (
                self._embedding_mode == "tokens"
                and len(tokens) * _DIMENSIONS[self._model_tier] > 2097152
            ):
                raise SchemaValidationError(
                    _MODEL_NAME,
                    [
                        "Embedding exceeds the context or token-output memory budget"
                    ],
                )
            spans = features.token_spans or ((0, len(tokens)),)
            if any(not 0 <= start < end <= len(tokens) for start, end in spans):
                raise SchemaValidationError(
                    _MODEL_NAME, ["Invalid contextual token span"]
                )
            try:
                native = context.embed(text, normalize=False, truncate=False)
                vectors = np.asarray(native, dtype=np.float32)
            except (ValueError, RuntimeError, TypeError) as error:
                logger.warning(
                    "embedding.inference.failed",
                    error_type=type(error).__name__,
                )
                raise SchemaValidationError(
                    _MODEL_NAME, ["Invalid native embedding output"]
                ) from None
            pooling = (
                "last" if self._embedding_mode == "pooled" else "late-mean"
            )
            profile = hashlib.sha256(
                f"qwen3:{self._model_tier}:{self._compute_type}:{self._weights_sha256}:{pooling}:{dimensions}:query-instruct-v1:l2".encode()
            ).hexdigest()
            if self._embedding_mode == "pooled":
                if vectors.ndim != 1 or vectors.shape[0] < dimensions:
                    raise SchemaValidationError(
                        _MODEL_NAME, ["Invalid pooled embedding shape"]
                    )
                embedding = self._normalize(vectors[:dimensions])
                prediction: dict[str, object] = {
                    "embedding": embedding.tolist(),
                    "dimension": dimensions,
                    "pooling": pooling,
                    "profile_id": profile,
                }
            else:
                if (
                    vectors.ndim != 2
                    or vectors.shape[0] != len(tokens)
                    or vectors.shape[1] < dimensions
                    or not np.isfinite(vectors).all()
                ):
                    raise SchemaValidationError(
                        _MODEL_NAME, ["Invalid contextual token matrix"]
                    )
                embeddings = [
                    self._normalize(
                        vectors[start:end, :dimensions].mean(axis=0)
                    ).tolist()
                    for start, end in spans
                ]
                prediction = {
                    "dimension": dimensions,
                    "pooling": pooling,
                    "profile_id": profile,
                }
                if features.token_spans:
                    prediction.update(
                        {
                            "embeddings": embeddings,
                            "token_spans": [list(span) for span in spans],
                        }
                    )
                else:
                    prediction["embedding"] = embeddings[0]
            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction=prediction,
                confidence=1.0,
            )

    @staticmethod
    def _normalize(
        vector: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
    ) -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
        norm = float(np.linalg.norm(vector))
        if (
            not np.isfinite(vector).all()
            or not np.isfinite(norm)
            or norm <= 1e-9
        ):
            raise SchemaValidationError(
                _MODEL_NAME, ["Embedding must be finite and nonzero"]
            )
        vector /= norm
        return vector

    def input_schema(self) -> dict[str, object]:
        return Features.model_json_schema()

    def output_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "embedding": {"type": "array", "items": {"type": "number"}},
                "embeddings": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                },
                "token_spans": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "integer"}},
                },
                "dimension": {"type": "integer"},
                "pooling": {"type": "string"},
                "profile_id": {"type": "string"},
            },
            "required": ["dimension", "pooling", "profile_id"],
            "oneOf": [
                {"required": ["embedding"]},
                {"required": ["embeddings", "token_spans"]},
            ],
        }
