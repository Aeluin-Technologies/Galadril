"""Reuse the local GLM-OCR adapter without loading GPU dependencies or remote inference."""

import io
import threading

import numpy as np
import structlog
from galadril_inference.common.types import PredictionRequest
from galadril_inference.models.text.glm_ocr import GlmOcrModel
from opentelemetry import trace
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from galadril_scribe.model_residency import ModelResidency

_tracer = trace.get_tracer("galadril.scribe.documents")
_REVISION = "2e85a62840ccac27daa451df36c736c4636b8628"


class _RecognizedText(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(max_length=65536)


class LocalOcr:
    __slots__ = (
        "_path",
        "_model",
        "_lock",
        "_loaded",
        "_residency",
        "_downloaded",
    )

    def __init__(
        self, path: str, *, residency: ModelResidency | None = None
    ) -> None:
        self._path = path
        self._model = GlmOcrModel(
            device="cpu",
            revision=_REVISION,
            default_max_new_tokens=2048,
            default_batch_size=1,
        )
        self._lock = threading.Lock()
        self._loaded = False
        self._downloaded = False
        self._residency = residency or ModelResidency()

    def _release(self) -> None:
        self._model.cleanup()
        self._loaded = False

    def read(self, image: bytes) -> str:
        """Keep one CPU model resident and serialize native inference outside asyncio."""
        with (
            self._lock,
            self._residency.use(self, self._release),
            _tracer.start_as_current_span("documents.ocr"),
        ):
            if not self._loaded:
                if not self._downloaded:
                    self._model.download(self._path)
                    self._downloaded = True
                self._model.load(self._path)
                self._loaded = True
                structlog.get_logger().info(
                    "documents.ocr_loaded", revision=_REVISION, device="cpu"
                )
            with Image.open(io.BytesIO(image)) as decoded:
                pixels = np.asarray(decoded.convert("RGB"), dtype=np.uint8)
            result = self._model.predict(
                PredictionRequest(
                    model_name="glm_ocr",
                    features={"image": pixels, "task": "text"},
                )
            )
            return _RecognizedText.model_validate(result.prediction).text
