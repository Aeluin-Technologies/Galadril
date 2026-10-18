"""A local multilingual cross-encoder reranks bounded query/passage batches."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Protocol, cast

import numpy as np
from pydantic import BaseModel as Payload
from pydantic import ConfigDict, Field, ValidationError

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

if TYPE_CHECKING:
    from tokenizers import Tokenizer

_REPOSITORY = "onnx-community/bge-reranker-v2-m3-ONNX"
_REVISION = "6f5ff65298512715a1e669753bc754d2bc8f367b"


class Features(Payload):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: str = Field(min_length=1, max_length=2048)
    documents: tuple[
        Annotated[str, Field(min_length=1, max_length=16384)], ...
    ] = Field(min_length=1, max_length=32)


class Input(Protocol):
    name: str


class Session(Protocol):
    def get_inputs(self) -> Sequence[Input]: ...
    def run(
        self,
        outputs: None,
        inputs: dict[str, np.ndarray[tuple[int, ...], np.dtype[np.int64]]],
    ) -> Sequence[object]: ...


class BgeRerankerModel(BaseModel):
    __slots__ = ("_session", "_tokenizer", "_lock", "profile_id")

    def __init__(self) -> None:
        self._session: Session | None = None
        self._tokenizer: Tokenizer | None = None
        self._lock = threading.Lock()
        self.profile_id = "unloaded"

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name="bge_reranker_v2_m3",
            version="1.0.0",
            description="Local multilingual cross-encoder passage relevance.",
            tags={"domain": "nlp", "backend": "onnxruntime"},
        )

    def download(self, target_path: str) -> None:
        from huggingface_hub import hf_hub_download

        for name in ("tokenizer.json", "onnx/model_quantized.onnx"):
            hf_hub_download(
                repo_id=_REPOSITORY,
                revision=_REVISION,
                filename=name,
                local_dir=target_path,
            )

    def load(self, artifact_path: str, device: str = "cpu") -> None:
        from tokenizers import Tokenizer

        root = Path(artifact_path)
        with self._lock:
            if self._session is not None:
                return
            if (
                not (root / "tokenizer.json").is_file()
                or not (root / "onnx/model_quantized.onnx").is_file()
            ):
                self.download(artifact_path)
            tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
            pad_id = tokenizer.token_to_id("<pad>")
            if pad_id is None:
                raise ModelLoadError(
                    "bge_reranker_v2_m3", "Tokenizer has no padding token"
                )
            tokenizer.enable_truncation(max_length=1024, strategy="only_second")
            tokenizer.enable_padding(pad_id=pad_id, pad_token="<pad>")
            with (root / "onnx/model_quantized.onnx").open("rb") as source:
                weights = hashlib.file_digest(source, "sha256").hexdigest()
            with (root / "tokenizer.json").open("rb") as source:
                vocabulary = hashlib.file_digest(source, "sha256").hexdigest()
            self.profile_id = hashlib.sha256(
                f"bge-reranker-v2-m3:int8:{weights}:{vocabulary}:1024:only-second:sigmoid".encode()
            ).hexdigest()
            self._session = cast(
                Session,
                create_session(
                    root / "onnx/model_quantized.onnx", device=device
                ),
            )
            self._tokenizer = tokenizer

    def cleanup(self) -> None:
        with self._lock:
            self._session = None
            self._tokenizer = None

    @staticmethod
    def normalize_logits(
        logits: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        *,
        expected: int,
    ) -> list[float]:
        values = np.asarray(logits, dtype=np.float32).reshape(-1)
        if len(values) != expected or not np.isfinite(values).all():
            raise ValueError("Invalid reranking output")
        np.clip(values, -80.0, 80.0, out=values)
        return [float(value) for value in 1.0 / (1.0 + np.exp(-values))]

    def predict(self, request: PredictionRequest) -> PredictionResult:
        try:
            features = Features.model_validate(request.features)
        except ValidationError:
            raise SchemaValidationError(
                "bge_reranker_v2_m3", ["Invalid query/passage batch"]
            ) from None
        with self._lock:
            if self._session is None or self._tokenizer is None:
                raise ModelLoadError(
                    "bge_reranker_v2_m3", "Reranker is not loaded"
                )
            try:
                pairs = [(features.query, text) for text in features.documents]
                encoded = self._tokenizer.encode_batch(pairs)
                names = {value.name for value in self._session.get_inputs()}
                inputs = {
                    name: np.asarray(
                        [getattr(item, attribute) for item in encoded],
                        dtype=np.int64,
                    )
                    for name, attribute in (
                        ("input_ids", "ids"),
                        ("attention_mask", "attention_mask"),
                        ("token_type_ids", "type_ids"),
                    )
                    if name in names
                }
                outputs = self._session.run(None, inputs)
                if not outputs:
                    raise ValueError("Missing reranking output")
                scores = self.normalize_logits(
                    np.asarray(outputs[0], dtype=np.float32),
                    expected=len(features.documents),
                )
            except (ValueError, RuntimeError, TypeError):
                raise SchemaValidationError(
                    "bge_reranker_v2_m3", ["Reranking failed"]
                ) from None
        return PredictionResult(
            model_name="bge_reranker_v2_m3",
            model_version="1.0.0",
            prediction={"scores": scores, "profile_id": self.profile_id},
            confidence=1.0,
        )

    def input_schema(self) -> dict[str, object]:
        return Features.model_json_schema()

    def output_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "scores": {"type": "array", "items": {"type": "number"}},
                "profile_id": {"type": "string"},
            },
            "required": ["scores", "profile_id"],
        }
