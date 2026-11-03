"""Recognize text locally with the CPU models bundled in the pinned OCR wheel."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from rapidocr import RapidOCR

_tracer = trace.get_tracer("galadril.scribe.documents")


class LocalOcr:
    __slots__ = ("_engine", "_lock")

    def __init__(self) -> None:
        self._engine: RapidOCR | None = None
        self._lock = threading.Lock()

    def read(self, image: bytes) -> str:
        """Reuse the small OCR sessions without evicting the embedding model."""
        with self._lock, _tracer.start_as_current_span("documents.ocr"):
            if self._engine is None:
                from rapidocr import RapidOCR

                self._engine = RapidOCR(
                    params={
                        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                    }
                )
            result = self._engine(image)
            return "\n".join(result.txts or ())[:65536]
