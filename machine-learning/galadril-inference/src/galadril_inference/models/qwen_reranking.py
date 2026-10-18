"""Qwen reranking evaluates its trained judge prompt using bounded native contexts."""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

import numpy as np

from galadril_inference.common.types import (
    ModelMeta,
    PredictionRequest,
    PredictionResult,
)
from galadril_inference.models.base import BaseModel
from galadril_inference.models.reranking import Features

_REPO = "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF"
_REVISION = "a02f48bb4f057028298c21fa033da2b30d7742d5"
_FILE = "qwen3-reranker-0.6b-q8_0.gguf"
_PREFIX = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_TASK = "Given a question, retrieve passages that contain evidence answering the question."


class Context(Protocol):
    def tokenize(
        self, text: bytes, *, add_bos: bool = False, special: bool = False
    ) -> list[int]: ...
    def reset(self) -> None: ...
    def eval(self, tokens: Sequence[int]) -> None: ...
    def sample(self, *, temp: float, logits_processor: object) -> int: ...
    def close(self) -> None: ...


class _Capture:
    __slots__ = ("yes", "no", "score")

    def __init__(self, yes: int, no: int) -> None:
        self.yes = yes
        self.no = no
        self.score: float | None = None

    def __call__(
        self,
        ids: np.ndarray[tuple[int, ...], np.dtype[np.intc]],
        scores: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
    ) -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
        if self.yes >= len(scores) or self.no >= len(scores):
            raise ValueError("Invalid judge token IDs")
        self.score = QwenRerankerModel.relevance(
            float(scores[self.yes]), float(scores[self.no])
        )
        return scores


class QwenRerankerModel(BaseModel):
    __slots__ = (
        "_ctx",
        "_lock",
        "_prefix",
        "_suffix",
        "_yes",
        "_no",
        "profile_id",
    )

    def __init__(self) -> None:
        self._ctx: Context | None = None
        self._lock = threading.RLock()
        self._prefix: list[int] = []
        self._suffix: list[int] = []
        self._yes = 0
        self._no = 0
        self.profile_id = "unloaded"

    def meta(self) -> ModelMeta:
        return ModelMeta(name="qwen3_reranker", version="1.0.0")

    @staticmethod
    def relevance(yes: float, no: float) -> float:
        if not math.isfinite(yes) or not math.isfinite(no):
            raise ValueError("Invalid reranking logits")
        difference = yes - no
        return (
            1 / (1 + math.exp(-difference))
            if difference >= 0
            else math.exp(difference) / (1 + math.exp(difference))
        )

    @staticmethod
    def document_budget(
        context: int, *, prefix: int, query: int, suffix: int
    ) -> int:
        remaining = context - prefix - query - suffix - 1
        if remaining < 1:
            raise ValueError("Reranking query exceeds its context")
        return remaining

    def download(self, target_path: str) -> None:
        from huggingface_hub import hf_hub_download

        hf_hub_download(
            repo_id=_REPO,
            revision=_REVISION,
            filename=_FILE,
            local_dir=target_path,
        )

    def load(self, artifact_path: str) -> None:
        from llama_cpp import Llama

        with self._lock:
            if self._ctx is not None:
                return
            path = Path(artifact_path) / _FILE
            if not path.is_file():
                self.download(artifact_path)
            with path.open("rb") as source:
                checksum = hashlib.file_digest(source, "sha256").hexdigest()
            context = cast(
                Context,
                Llama(
                    model_path=str(path),
                    n_ctx=4096,
                    n_batch=128,
                    n_ubatch=128,
                    n_gpu_layers=0,
                    logits_all=False,
                    verbose=False,
                ),
            )
            yes = context.tokenize(b"yes", add_bos=False, special=False)
            no = context.tokenize(b"no", add_bos=False, special=False)
            if len(yes) != 1 or len(no) != 1:
                context.close()
                raise ValueError("Judge answers must each be a single token")
            self._yes = yes[0]
            self._no = no[0]
            self._prefix = context.tokenize(
                _PREFIX.encode(), add_bos=False, special=True
            )
            self._suffix = context.tokenize(
                _SUFFIX.encode(), add_bos=False, special=True
            )
            self.profile_id = hashlib.sha256(
                f"qwen3-reranker:0.6b:q8_0:{checksum}:4096:document-tail-truncate:{_TASK}:{_PREFIX}:{_SUFFIX}:yes-no-softmax".encode()
            ).hexdigest()
            self._ctx = context

    def predict(self, request: PredictionRequest) -> PredictionResult:
        from llama_cpp import LogitsProcessorList

        features = Features.model_validate(request.features)
        with self._lock:
            context = self._ctx
            if context is None:
                raise ValueError("Reranker is not loaded")
            query = context.tokenize(
                f"<Instruct>: {_TASK}\n<Query>: {features.query}\n<Document>: ".encode(),
                add_bos=False,
                special=False,
            )
            budget = self.document_budget(
                4096,
                prefix=len(self._prefix),
                query=len(query),
                suffix=len(self._suffix),
            )
            scores = []
            for document in features.documents:
                tokens = context.tokenize(
                    document.encode(), add_bos=False, special=False
                )
                context.reset()
                context.eval(
                    [*self._prefix, *query, *tokens[:budget], *self._suffix]
                )
                capture = _Capture(self._yes, self._no)
                context.sample(
                    temp=0.0, logits_processor=LogitsProcessorList([capture])
                )
                if capture.score is None:
                    raise ValueError("Judge logits are unavailable")
                scores.append(capture.score)
            return PredictionResult(
                model_name="qwen3_reranker",
                model_version="1.0.0",
                prediction={"scores": scores, "profile_id": self.profile_id},
            )

    def cleanup(self) -> None:
        with self._lock:
            if self._ctx is not None:
                self._ctx.close()
                self._ctx = None

    def input_schema(self) -> dict[str, object]:
        return Features.model_json_schema()

    def output_schema(self) -> dict[str, object]:
        return {"type": "object", "required": ["scores", "profile_id"]}
