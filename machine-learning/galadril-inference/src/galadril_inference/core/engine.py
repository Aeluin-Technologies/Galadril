"""Single entry point for the library."""

from __future__ import annotations

import importlib
import pkgutil
import time
from typing import Any
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
        logger.info("engine_initialized")

    def _import_model_lazily(self, target_name: str) -> None:
        """Lazily import model modules until the target model is discovered."""
        if hasattr(self._registry, "discover"):
            self._registry.discover()
        if any(
            meta.name == target_name for meta in self._registry.list_models()
        ):
            return

        for module_info in pkgutil.walk_packages(
            _models_pkg.__path__,
            prefix=_models_pkg.__name__ + ".",
        ):
            if any(
                skip in module_info.name
                for skip in [".tests", ".utils", ".benchmarks", "test_"]
            ):
                continue

            try:
                importlib.import_module(module_info.name)
                if hasattr(self._registry, "discover"):
                    self._registry.discover()
                if any(
                    meta.name == target_name
                    for meta in self._registry.list_models()
                ):
                    return
            except Exception:
                logger.exception(
                    "model_module_import_failed", module=module_info.name
                )

        for module_info in pkgutil.iter_modules():
            if module_info.name.startswith(
                ("_", "test", "distutils", "pydoc", "unittest", "encodings")
            ):
                continue
            if module_info.name in (
                "boto3",
                "botocore",
                "daft",
                "numpy",
                "cv2",
                "asyncio",
                "structlog",
                "threading",
                "tempfile",
                "shutil",
                "hashlib",
                "importlib",
                "pkgutil",
                "inspect",
                "sys",
                "os",
                "pathlib",
                "typing",
                "collections",
                "traceback",
                "uuid",
                "datetime",
                "orjson",
                "ray",
            ):
                continue

            try:
                if module_info.ispkg:
                    if any(
                        kw in module_info.name
                        for kw in ("model", "inference", "plugin", "galadril")
                    ):
                        mod = importlib.import_module(module_info.name)
                        if hasattr(mod, "__path__"):
                            for sub_info in pkgutil.walk_packages(
                                mod.__path__, prefix=mod.__name__ + "."
                            ):
                                try:
                                    importlib.import_module(sub_info.name)
                                    if hasattr(self._registry, "discover"):
                                        self._registry.discover()
                                    if any(
                                        meta.name == target_name
                                        for meta in self._registry.list_models()
                                    ):
                                        return
                                except Exception:
                                    pass
                    else:
                        try:
                            models_submodule = f"{module_info.name}.models"
                            importlib.import_module(models_submodule)
                            models_mod = importlib.import_module(
                                models_submodule
                            )
                            if hasattr(models_mod, "__path__"):
                                for sub_info in pkgutil.walk_packages(
                                    models_mod.__path__,
                                    prefix=models_mod.__name__ + ".",
                                ):
                                    try:
                                        importlib.import_module(sub_info.name)
                                        if hasattr(self._registry, "discover"):
                                            self._registry.discover()
                                        if any(
                                            meta.name == target_name
                                            for meta in self._registry.list_models()
                                        ):
                                            return
                                    except Exception:
                                        pass
                        except ImportError:
                            pass
                else:
                    if (
                        "model" in module_info.name
                        or "inference" in module_info.name
                    ):
                        importlib.import_module(module_info.name)
                        if hasattr(self._registry, "discover"):
                            self._registry.discover()
                        if any(
                            meta.name == target_name
                            for meta in self._registry.list_models()
                        ):
                            return
            except Exception:
                pass

        if hasattr(self._registry, "discover"):
            self._registry.discover()

    def _import_all_model_modules_full(self) -> None:
        """Full scan fallback used only when a total system discovery is requested."""
        for module_info in pkgutil.walk_packages(
            _models_pkg.__path__,
            prefix=_models_pkg.__name__ + ".",
        ):
            if any(
                skip in module_info.name
                for skip in [".tests", ".utils", ".benchmarks", "test_"]
            ):
                continue
            try:
                importlib.import_module(module_info.name)
            except Exception:
                logger.exception(
                    "model_module_import_failed", module=module_info.name
                )

        for module_info in pkgutil.iter_modules():
            if module_info.name.startswith(
                ("_", "test", "distutils", "pydoc", "unittest", "encodings")
            ):
                continue
            if module_info.name in (
                "boto3",
                "botocore",
                "daft",
                "numpy",
                "cv2",
                "asyncio",
                "structlog",
                "threading",
                "tempfile",
                "shutil",
                "hashlib",
                "importlib",
                "pkgutil",
                "inspect",
                "sys",
                "os",
                "pathlib",
                "typing",
                "collections",
                "traceback",
                "uuid",
                "datetime",
                "orjson",
                "ray",
            ):
                continue
            try:
                if module_info.ispkg:
                    if any(
                        kw in module_info.name
                        for kw in ("model", "inference", "plugin", "galadril")
                    ):
                        mod = importlib.import_module(module_info.name)
                        if hasattr(mod, "__path__"):
                            for sub_info in pkgutil.walk_packages(
                                mod.__path__, prefix=mod.__name__ + "."
                            ):
                                try:
                                    importlib.import_module(sub_info.name)
                                except Exception:
                                    pass
                else:
                    if (
                        "model" in module_info.name
                        or "inference" in module_info.name
                    ):
                        importlib.import_module(module_info.name)
            except Exception:
                pass

        if hasattr(self._registry, "discover"):
            self._registry.discover()

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
        if not any(meta.name == name for meta in self._registry.list_models()):
            self._import_model_lazily(name)

        model = self._registry.get(name)
        meta = model.meta()

        if self._registry.status(name) == ModelStatus.READY:
            logger.debug("model_already_loaded", name=name)
            return

        self._registry.set_status(name, ModelStatus.LOADING)

        try:
            if not self._loader.exists(meta.name, meta.version):
                logger.warning(
                    "model_missing_on_remote_storage_starting_bootstrap",
                    name=meta.name,
                    version=meta.version,
                )
                import tempfile
                import os

                with tempfile.TemporaryDirectory() as tmpdir:
                    logger.info(
                        "instantiating_model_for_upstream_download",
                        model_name=meta.name,
                        version=meta.version,
                    )
                    model.download(tmpdir)

                    logger.info(
                        "uploading_bootstrapped_artifacts_to_remote",
                        name=meta.name,
                        version=meta.version,
                    )

                    if hasattr(self._loader, "upload"):
                        self._loader.upload(meta.name, meta.version, tmpdir)
                    elif hasattr(self._loader, "bucket") and hasattr(
                        self._loader, "prefix"
                    ):
                        import boto3

                        s3_client = boto3.client(
                            "s3",
                            endpoint_url=getattr(
                                self._loader, "endpoint_url", None
                            ),
                        )
                        bucket = self._loader.bucket
                        remote_prefix = f"{self._loader.prefix}/{meta.name}/{meta.version}".strip(
                            "/"
                        )

                        for root, _, files in os.walk(tmpdir):
                            for file in files:
                                local_file_path = os.path.join(root, file)
                                relative_path = os.path.relpath(
                                    local_file_path, tmpdir
                                )
                                s3_key = f"{remote_prefix}/{relative_path}"
                                s3_client.upload_file(
                                    local_file_path, bucket, s3_key
                                )
                    else:
                        raise NotImplementedError(
                            f"The artifact loader '{type(self._loader).__name__}' does not implement 'upload' "
                            f"and no agnostical S3 fallback layout could be determined."
                        )

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
        self._import_all_model_modules_full()
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

        if not any(meta.name == name for meta in self._registry.list_models()):
            self._import_model_lazily(name)

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
