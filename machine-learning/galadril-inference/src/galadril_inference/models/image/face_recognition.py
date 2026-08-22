"""Face recognition model powered by InsightFace (Buffalo)."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
from galadril_inference.models.runtime import resolve_providers

logger = structlog.get_logger(__name__)

_MODEL_NAME = "face_recognition"
_MODEL_VERSION = "1.0.0"


@unique
class FaceAction(StrEnum):
    """Supported inference actions for the face recognition model."""

    DETECT = "detect"
    EMBED = "embed"


@dataclass(frozen=True, slots=True)
class DetectedFace:
    """A single face detected in an image."""

    bbox: list[float]
    keypoints: list[list[float]]
    confidence: float
    embedding: list[float] | None = None


class FaceRecognitionModel(BaseModel):
    """InsightFace-based face recognition model."""

    def __init__(self) -> None:
        self._app: Any | None = None

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description=(
                "Face detection, embedding, and recognition via InsightFace."
            ),
            tags={
                "domain": "vision",
                "backend": "insightface",
                "model_pack": "buffalo_l",
            },
        )

    def download(self, target_path: str) -> None:
        """Download the buffalo_l model artifacts from the upstream source.

        Agnostically fetches files from the official InsightFace distribution
        and places them in the structural layout expected by FaceAnalysis.
        """
        import urllib.request
        import zipfile

        download_dir = os.path.join(
            target_path, ".insightface", "models", "buffalo_l"
        )
        os.makedirs(download_dir, exist_ok=True)

        upstream_url = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
        zip_tmp_path = os.path.join(target_path, "buffalo_l.zip")

        logger.info(
            "downloading_upstream_model_artifacts",
            model_name=_MODEL_NAME,
            url=upstream_url,
            target=zip_tmp_path,
        )

        try:
            urllib.request.urlretrieve(upstream_url, zip_tmp_path)
            with zipfile.ZipFile(zip_tmp_path, "r") as zip_ref:
                zip_ref.extractall(download_dir)

            logger.info(
                "upstream_model_artifacts_downloaded_and_extracted",
                model_name=_MODEL_NAME,
                extracted_path=download_dir,
            )
        except Exception as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                f"Failed to bootstrap and download upstream model artifacts: {exc}",
            ) from exc
        finally:
            if os.path.exists(zip_tmp_path):
                os.remove(zip_tmp_path)

    def load(self, artifact_path: str, device: str = "auto") -> None:
        """Load the InsightFace model pack from the artifact directory."""
        try:
            import onnxruntime as ort
            from insightface.app import FaceAnalysis
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME,
                "insightface is not installed. "
                "Install it with: pip install insightface onnxruntime",
            ) from exc

        try:
            root = self._resolve_artifact_root(artifact_path)
            providers = resolve_providers(ort.get_available_providers(), device)
            self._app = FaceAnalysis(
                name="buffalo_l",
                root=str(root),
                allowed_modules=["detection", "recognition"],
                providers=list(providers),
            )
            ctx_id = -1 if providers == ("CPUExecutionProvider",) else 0
            self._app.prepare(ctx_id=ctx_id, det_size=(640, 640))

            logger.info(
                "model_loaded",
                artifact_path=str(root),
                model_count=len(self._app.models),
                model_name=_MODEL_NAME,
                providers=providers,
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release the model from memory."""
        self._app = None
        logger.info("model_cleaned_up", model_name=_MODEL_NAME)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Dispatch to the appropriate action handler."""
        self._ensure_loaded()
        action = self._extract_action(request)

        match action:
            case FaceAction.DETECT:
                return self._predict_detect(request)
            case FaceAction.EMBED:
                return self._predict_embed(request)

    def _predict_detect(self, request: PredictionRequest) -> PredictionResult:
        """Detect faces and return bounding boxes + keypoints."""
        image = self._extract_image(request, key="image")
        faces = self._app.get(image)

        if not faces:
            return self._build_result({"faces_count": 0, "faces": []}, 0.0)

        results = [
            {
                "bbox": face.bbox.tolist(),
                "keypoints": face.kps.tolist() if face.kps is not None else [],
                "confidence": float(face.det_score),
            }
            for face in faces
        ]

        avg_conf = float(np.mean([f.det_score for f in faces]))

        return self._build_result(
            prediction={
                "faces_count": len(results),
                "faces": results,
            },
            confidence=round(avg_conf, 6),
        )

    def _predict_embed(self, request: PredictionRequest) -> PredictionResult:
        """Detect faces and extract 512-d embeddings."""
        image = self._extract_image(request, key="image")
        faces = self._app.get(image)

        if not faces:
            return self._build_result({"faces_count": 0, "faces": []}, 0.0)

        results = []
        for face in faces:
            emb = face.embedding
            results.append(
                {
                    "bbox": face.bbox.tolist(),
                    "confidence": float(face.det_score),
                    "embedding": emb.tolist() if emb is not None else None,
                    "embedding_dim": emb.shape[0] if emb is not None else 0,
                }
            )

        avg_conf = float(np.mean([f.det_score for f in faces]))

        return self._build_result(
            prediction={
                "faces_count": len(results),
                "faces": results,
            },
            confidence=round(avg_conf, 6),
        )

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["action", "image"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [a.value for a in FaceAction],
                    "description": "The inference action to perform.",
                },
                "image": {
                    "type": "ndarray",
                    "description": "BGR image as numpy array (H, W, 3).",
                },
            },
        }

    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "faces_count": {"type": "integer"},
                "faces": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "bbox": {
                                "type": "array",
                                "items": {"type": "number"},
                            },
                            "keypoints": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                },
                            },
                            "confidence": {"type": "number"},
                            "embedding": {
                                "type": "array",
                                "items": {"type": "number"},
                            },
                        },
                    },
                },
            },
        }

    @staticmethod
    def _resolve_artifact_root(artifact_path: str) -> Path:
        """Resolve the InsightFace cache root from a versioned artifact path."""
        base_path = Path(artifact_path)
        insightface_root = base_path / ".insightface"

        if (insightface_root / "models" / "buffalo_l").is_dir():
            return insightface_root
        if (base_path / "models" / "buffalo_l").is_dir():
            return base_path
        if insightface_root.exists():
            return insightface_root
        return base_path

    def _ensure_loaded(self) -> None:
        if self._app is None:
            raise ModelLoadError(
                _MODEL_NAME,
                "Model is not loaded. Call load() before predict().",
            )

    @staticmethod
    def _extract_action(request: PredictionRequest) -> FaceAction:
        raw_action = request.features.get("action")
        if raw_action is None:
            raise SchemaValidationError(
                _MODEL_NAME,
                ["Missing required feature: 'action'."],
            )
        try:
            return FaceAction(raw_action)
        except ValueError as exc:
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Invalid action '{raw_action}'. "
                    f"Must be one of: {[a.value for a in FaceAction]}.",
                ],
            ) from exc

    @staticmethod
    def _extract_image(
        request: PredictionRequest,
        *,
        key: str,
    ) -> NDArray[np.uint8]:
        image = request.features.get(key)
        if image is None:
            raise SchemaValidationError(
                _MODEL_NAME,
                [f"Missing required feature: '{key}'."],
            )
        if not isinstance(image, np.ndarray):
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Feature '{key}' must be a numpy ndarray, "
                    f"got {type(image).__name__}."
                ],
            )
        if image.ndim != 3 or image.shape[2] != 3:
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Feature '{key}' must have shape (H, W, 3), got "
                    f"{image.shape}.",
                ],
            )
        return image

    @staticmethod
    def _avg_confidence(faces: list[DetectedFace]) -> float:
        if not faces:
            return 0.0
        return round(sum(f.confidence for f in faces) / len(faces), 6)

    def _build_result(
        self,
        prediction: dict[str, Any],
        confidence: float,
    ) -> PredictionResult:
        return PredictionResult(
            model_name=_MODEL_NAME,
            model_version=_MODEL_VERSION,
            prediction=prediction,
            confidence=max(0.0, min(1.0, confidence)),
        )
