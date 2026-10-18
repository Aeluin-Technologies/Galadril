"""Local native models are loaded lazily and serialized outside the event loop."""

import asyncio
import hashlib
import threading
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from galadril_scribe.retrieval import structural_chunks
from galadril_scribe.retrieval_contracts import (
    EmbeddedTexts,
    EmbedTexts,
    TextUnit,
)

_pdf_lock = threading.Lock()
_TASK = "Given a question, retrieve passages that contain evidence answering the question."


class _Embedding(BaseModel):
    model_config = ConfigDict(extra="ignore")
    embedding: tuple[FiniteFloat, ...] = Field(min_length=1, max_length=4096)
    profile_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class _Scores(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scores: tuple[FiniteFloat, ...] = Field(max_length=32)


class LocalEmbeddings:
    __slots__ = ("path", "_model", "_lock")

    def __init__(self, path: str) -> None:
        from galadril_inference.models.embedding.qwen3 import (
            Qwen3EmbeddingModel,
        )

        self.path = path
        self._model = Qwen3EmbeddingModel()
        self._lock = threading.Lock()

    def _embed(self, request: EmbedTexts) -> EmbeddedTexts:
        from galadril_inference.common.types import PredictionRequest

        with self._lock:
            self._model.load(
                self.path,
                model_tier="0.6b",
                compute_type="q8_0",
                n_gpu_layers=0,
                n_ctx=8192,
            )
            results = tuple(
                _Embedding.model_validate(
                    self._model.predict(
                        PredictionRequest(
                            model_name="qwen_embedding",
                            features={
                                "text": text,
                                "task": _TASK if request.query else None,
                                "dimensions": 1024,
                            },
                        )
                    ).prediction
                )
                for text in request.texts
            )
            profile = hashlib.sha256(
                f"document-v1:{results[0].profile_id}:{_TASK}".encode()
            ).hexdigest()
            if any(
                item.profile_id != results[0].profile_id for item in results
            ):
                raise ValueError("Embedding profile changed")
            return EmbeddedTexts(
                profile_id=profile,
                vectors=tuple(item.embedding for item in results),
            )

    async def embed(self, request: EmbedTexts) -> EmbeddedTexts:
        return await asyncio.to_thread(self._embed, request)


class LocalReranker:
    __slots__ = ("path", "_model", "_lock", "_name")

    def __init__(
        self, path: str, *, backend: Literal["bge", "qwen"] = "bge"
    ) -> None:
        from galadril_inference.models.qwen_reranking import QwenRerankerModel
        from galadril_inference.models.reranking import BgeRerankerModel

        self.path = path
        self._name = (
            "qwen3_reranker" if backend == "qwen" else "bge_reranker_v2_m3"
        )
        self._model = (
            QwenRerankerModel() if backend == "qwen" else BgeRerankerModel()
        )
        self._lock = threading.Lock()

    @property
    def profile_id(self) -> str:
        return self._model.profile_id

    def _scores(
        self, query: str, documents: tuple[str, ...]
    ) -> tuple[float, ...]:
        from galadril_inference.common.types import PredictionRequest

        with self._lock:
            self._model.load(self.path)
            return tuple(
                _Scores.model_validate(
                    self._model.predict(
                        PredictionRequest(
                            model_name=self._name,
                            features={"query": query, "documents": documents},
                        )
                    ).prediction
                ).scores
            )

    async def scores(
        self, query: str, documents: tuple[str, ...]
    ) -> tuple[float, ...]:
        return await asyncio.to_thread(self._scores, query, documents)


class _TextPage(Protocol):
    def count_chars(self) -> int: ...
    def get_text_range(self) -> str: ...
    def close(self) -> None: ...


class _Page(Protocol):
    def get_textpage(self) -> _TextPage: ...
    def close(self) -> None: ...


class _Document(Protocol):
    def __len__(self) -> int: ...
    def get_page(self, index: int) -> _Page: ...
    def close(self) -> None: ...


class PdfParser:
    __slots__ = ()

    def _parse(self, content: bytes, mime: str) -> tuple[TextUnit, ...]:
        import pypdfium2

        if mime != "application/pdf" or not content.startswith(b"%PDF-"):
            raise ValueError("OCR is required for this document")
        units: list[TextUnit] = []
        remaining = 1048576
        # PDFium forbids concurrent calls even across independent documents.
        with _pdf_lock:
            document = cast(_Document, pypdfium2.PdfDocument(content))
            try:
                if len(document) > 256:
                    raise ValueError("PDF page budget exceeded")
                for index in range(len(document)):
                    page = document.get_page(index)
                    try:
                        textpage = page.get_textpage()
                        try:
                            if textpage.count_chars() > remaining:
                                raise ValueError("PDF text budget exceeded")
                            text = textpage.get_text_range()
                        finally:
                            textpage.close()
                    finally:
                        page.close()
                    remaining -= len(text.encode())
                    if remaining < 0:
                        raise ValueError("PDF text budget exceeded")
                    for chunk in structural_chunks(text):
                        units.append(
                            TextUnit(
                                ordinal=len(units),
                                text=chunk.text,
                                start=chunk.start,
                                end=chunk.end,
                                page=index + 1,
                            )
                        )
                    if len(units) > 2048:
                        raise ValueError("PDF chunk budget exceeded")
            finally:
                document.close()
        return tuple(units)

    async def parse(self, content: bytes, mime: str) -> tuple[TextUnit, ...]:
        return await asyncio.to_thread(self._parse, content, mime)
