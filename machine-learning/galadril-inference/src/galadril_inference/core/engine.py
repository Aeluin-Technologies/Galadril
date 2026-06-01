"""Single entry point for the library."""

from __future__ import annotations

import importlib
import pkgutil
import time
from collections.abc import Sequence

import structlog

import galadril_inference.models as _models_pkg
from galadril_inference.common.exceptions import (
    ModelLoadError,
    ModelNotReadyError,
)
from galadril_inference.common.types import (
    ModelMeta,
    ModelStatus,
    ModelSummary,
    PredictionRequest,
    PredictionResult,
)
from galadril_inference.core.registry import ModelRegistry
from galadril_inference.loading.loader import ArtifactLoader

logger = structlog.get_logger(__name__)


class InferenceEngine:
    """High-level API for model lifecycle management and inference."""

    def __init__(self, loader: ArtifactLoader) -> None:
        self._loader = loader
        self._registry = ModelRegistry()

        self._import_all_model_modules()
        count = self._registry.discover()
        logger.info("engine_initialized", model_count=count)

    def load_model(self, name: str, **kwargs: Any) -> None:
        """Load a single model's artifacts into memory with optional custom parameters.

        Args:
            name: The unique identifier of the model to load.
            **kwargs: Optional configuration parameters passed directly to the
                model's custom load implementation (e.g., model_tier, compute_type).

        Raises:
            ModelNotFoundError: if the model name is unknown.
            ModelLoadError: if artifact loading fails.
        """
        model = self._registry.get(name)
        meta = model.meta()

        if self._registry.status(name) == ModelStatus.READY:
            logger.debug("model_already_loaded", name=name)
            return

        self._registry.set_status(name, ModelStatus.LOADING)

        try:
            artifact_path = self._loader.resolve(meta.name, meta.version)
            model.load(artifact_path, **kwargs)
        except Exception as exc:
            self._registry.set_status(name, ModelStatus.ERROR)
            raise ModelLoadError(name, str(exc)) from exc

        self._registry.set_status(name, ModelStatus.READY)
        logger.info("model_ready", name=meta.name, version=meta.version)

    def load_all(self) -> None:
        """Load every discovered model. Errors are collected, not raised.

        Returns silently. Check individual model status via :meth:`model_status`
        to find models that failed to load.
        """
        for meta in self._registry.list_models():
            try:
                self.load_model(meta.name)
            except ModelLoadError:
                logger.exception("model_load_skipped", name=meta.name)

    def unload_model(self, name: str) -> None:
        """Unload a model and release its resources."""
        model = self._registry.get(name)
        model.cleanup()
        self._registry.set_status(name, ModelStatus.UNLOADED)
        logger.info("model_unloaded", name=name)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run inference on a single request.

        Raises:
            ModelNotFoundError: if the model name is unknown.
            ModelNotReadyError: if the model has not been loaded yet.
        """
        name = request.model_name
        status = self._registry.status(name)

        if status != ModelStatus.READY:
            raise ModelNotReadyError(
                f"Model '{name}' is not ready (status: {status}). "
                f"Call engine.load_model('{name}') first."
            )

        model = self._registry.get(name)

        start = time.perf_counter()
        result = model.predict(request)
        elapsed_ms = (time.perf_counter() - start) * 1000

        return PredictionResult(
            model_name=result.model_name,
            model_version=result.model_version,
            prediction=result.prediction,
            confidence=result.confidence,
            request_id=request.request_id,
            latency_ms=round(elapsed_ms, 3),
        )

    def list_models(self) -> list[ModelMeta]:
        """Return metadata for all discovered models."""
        return self._registry.list_models()

    def list_model_summaries(self) -> list[ModelSummary]:
        """Return lightweight info for API listing endpoints."""
        summaries: list[ModelSummary] = []
        for meta in self._registry.list_models():
            summaries.append(
                ModelSummary(
                    name=meta.name,
                    version=meta.version,
                    description=meta.description,
                    deprecated=meta.deprecated,
                )
            )
        return summaries

    def categories_index(self) -> dict[str, list[str]]:
        """Return category -> model name index."""
        return self._registry.categories_index()

    def model_status(self, name: str) -> ModelStatus:
        """Return the lifecycle status of a specific model."""
        return self._registry.status(name)

    def ready_models(self) -> Sequence[str]:
        """Return names of all models currently in READY state."""
        return [
            meta.name
            for meta in self._registry.list_models()
            if self._registry.status(meta.name) == ModelStatus.READY
        ]

    @staticmethod
    def _import_all_model_modules() -> None:
        """Auto-import every module under galadril_inference.models."""
        for module_info in pkgutil.walk_packages(
            _models_pkg.__path__,
            prefix=_models_pkg.__name__ + ".",
        ):
            try:
                importlib.import_module(module_info.name)
            except Exception:
                logger.exception(
                    "model_module_import_failed", module=module_info.name
                )
