"""Local native models are loaded lazily and serialized outside the event loop."""

import asyncio
import base64
import hashlib
import io
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
        from galadril_inference.models.embedding.qwen_vl import (
            Qwen3VLEmbeddingModel,
        )

        self.path = path
        self._model = Qwen3VLEmbeddingModel()
        self._lock = threading.Lock()

    def _embed(self, request: EmbedTexts) -> EmbeddedTexts:
        with self._lock:
            self._model.load(self.path)
            vectors = tuple(
                self._model.embed(
                    text,
                    query=request.query,
                    image=base64.b64decode(request.images[index], validate=True)
                    if request.images and request.images[index] is not None
                    else None,
                )
                for index, text in enumerate(request.texts)
            )
            return EmbeddedTexts(
                profile_id=self._model.profile_id, vectors=vectors
            )

    async def embed(self, request: EmbedTexts) -> EmbeddedTexts:
        worker = asyncio.create_task(asyncio.to_thread(self._embed, request))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await worker
            raise


class LegacyTextEmbeddings:
    """Reproduce historical cached benchmarks without mixing vector spaces."""

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

        if any(request.images):
            raise ValueError("Historical text profiles do not support images")
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


class Ocr(Protocol):
    def read(self, image: bytes) -> str: ...


class _Bitmap(Protocol):
    def to_pil(self) -> object: ...
    def close(self) -> None: ...


class _Page(Protocol):
    def get_size(self) -> tuple[float, float]: ...
    def render(self, *, scale: float) -> _Bitmap: ...
    def get_textpage(self) -> _TextPage: ...
    def close(self) -> None: ...


class _Document(Protocol):
    def __len__(self) -> int: ...
    def get_page(self, index: int) -> _Page: ...
    def close(self) -> None: ...


class PdfParser:
    __slots__ = ("_ocr",)

    def __init__(self, *, ocr: Ocr | None = None) -> None:
        self._ocr = ocr

    def _image_text(self, content: bytes) -> str:
        from PIL import Image

        if self._ocr is None:
            raise ValueError("Local OCR is not configured")
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > 16_000_000:
                    raise ValueError("Image pixel budget exceeded")
                image.thumbnail((1600, 1600))
                output = io.BytesIO()
                image.convert("RGB").save(output, format="PNG")
        except (OSError, Image.DecompressionBombError):
            raise ValueError("Image decoding rejected") from None
        return self._ocr.read(output.getvalue())

    def _parse(self, content: bytes, mime: str) -> tuple[TextUnit, ...]:
        import pypdfium2
        from PIL import Image

        if mime in {"image/png", "image/jpeg", "image/webp"}:
            return tuple(
                TextUnit(
                    ordinal=index,
                    text=chunk.text,
                    start=chunk.start,
                    end=chunk.end,
                    page=1,
                )
                for index, chunk in enumerate(
                    structural_chunks(self._image_text(content) or "[Image]")
                )
            )
        if mime != "application/pdf" or not content.startswith(b"%PDF-"):
            raise ValueError("Unsupported document format")
        units: list[TextUnit] = []
        remaining = 1048576
        # Serialize native PDFium calls; OCR never owns the global PDFium lock.
        with _pdf_lock:
            document = cast(_Document, pypdfium2.PdfDocument(content))
            page_count = len(document)
        try:
            if page_count > 256:
                raise ValueError("PDF page budget exceeded")
            for index in range(page_count):
                image_content: bytes | None = None
                with _pdf_lock:
                    page = document.get_page(index)
                    try:
                        textpage = page.get_textpage()
                        try:
                            if textpage.count_chars() > remaining:
                                raise ValueError("PDF text budget exceeded")
                            text = textpage.get_text_range()
                        finally:
                            textpage.close()
                        if not text.strip():
                            width, height = page.get_size()
                            if width <= 0 or height <= 0:
                                raise ValueError("Invalid PDF page size")
                            bitmap = page.render(
                                scale=min(2.0, 1600 / max(width, height))
                            )
                            try:
                                image = cast(Image.Image, bitmap.to_pil())
                                output = io.BytesIO()
                                image.save(output, format="PNG")
                                image_content = output.getvalue()
                            finally:
                                bitmap.close()
                    finally:
                        page.close()
                if image_content is not None:
                    text = self._image_text(image_content)
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
            with _pdf_lock:
                document.close()
        return tuple(units)

    async def parse(self, content: bytes, mime: str) -> tuple[TextUnit, ...]:
        worker = asyncio.create_task(
            asyncio.to_thread(self._parse, content, mime)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Native OCR cannot be interrupted; retain API admission until it releases model memory.
            await worker
            raise
