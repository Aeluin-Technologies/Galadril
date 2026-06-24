"""Inference engine core entry point."""

from __future__ import annotations

import asyncio
import importlib
import os
import pkgutil
import shutil
import tempfile
import time
from collections.abc import Sequence
from typing import Any

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
    """Manages model lifecycles and handles prediction orchestration."""

    def __init__(self, loader: ArtifactLoader) -> None:
        """Initializes the inference engine.

        Args:
            loader: The artifact loader backend instance.
        """
        self._loader = loader
        self._registry = ModelRegistry()
        logger.info("engine_initialized")

    def _import_model_lazily(self, target_name: str) -> None:
        """Dynamically scans and imports modules until the target model is found.

        Args:
            target_name: The name of the model to search for.
        """
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
        """Performs a comprehensive scan and imports all model modules."""
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

    async def load_model(self, name: str, **kwargs: Any) -> None:
        """Loads a model's artifacts into memory, bootstrapping from the backend if required.

        Args:
            name: Name of the model to load.
            **kwargs: Additional parameters passed to the model's load hook.

        Raises:
            ModelLoadError: If loading, downloading, or bootstrapping fails.
        """
        if not any(meta.name == name for meta in self._registry.list_models()):
            await asyncio.to_thread(self._import_model_lazily, name)

        model = self._registry.get(name)
        meta = model.meta()

        if self._registry.status(name) == ModelStatus.READY:
            logger.debug("model_already_loaded", name=name)
            return

        self._registry.set_status(name, ModelStatus.LOADING)

        try:
            if not await self._loader.exists(meta.name, meta.version):
                logger.warning(
                    "model_missing_on_remote_storage_starting_bootstrap",
                    name=meta.name,
                    version=meta.version,
                )

                tmpdir = await asyncio.to_thread(tempfile.mkdtemp)
                try:
                    logger.info(
                        "instantiating_model_for_upstream_download",
                        model_name=meta.name,
                        version=meta.version,
                    )
                    await asyncio.to_thread(model.download, tmpdir)

                    logger.info(
                        "uploading_bootstrapped_artifacts_to_remote",
                        name=meta.name,
                        version=meta.version,
                    )

                    if hasattr(self._loader, "upload"):
                        await self._loader.upload(
                            meta.name, meta.version, tmpdir
                        )
                    elif hasattr(self._loader, "bucket") and hasattr(
                        self._loader, "prefix"
                    ):
                        import aioboto3

                        session = aioboto3.Session()
                        endpoint_url = getattr(
                            self._loader, "endpoint_url", None
                        )
                        bucket = getattr(self._loader, "bucket")
                        remote_prefix = f"{getattr(self._loader, 'prefix')}/{meta.name}/{meta.version}".strip(
                            "/"
                        )

                        def _collect_local_files() -> list[tuple[str, str]]:
                            collected = []
                            for root, _, files in os.walk(tmpdir):
                                for file in files:
                                    local_file_path = os.path.join(root, file)
                                    relative_path = os.path.relpath(
                                        local_file_path, tmpdir
                                    )
                                    collected.append(
                                        (local_file_path, relative_path)
                                    )
                            return collected

                        local_files = await asyncio.to_thread(
                            _collect_local_files
                        )
                        io_semaphore = asyncio.Semaphore(10)

                        client_context: Any = session.client(
                            "s3", endpoint_url=endpoint_url
                        )
                        async with client_context as s3_client:

                            async def _upload_task(
                                local_path: str, rel_path: str
                            ) -> None:
                                s3_key = f"{remote_prefix}/{rel_path}"
                                async with io_semaphore:
                                    await s3_client.upload_file(
                                        local_path, bucket, s3_key
                                    )

                            await asyncio.gather(
                                *(
                                    _upload_task(lp, rp)
                                    for lp, rp in local_files
                                )
                            )
                    else:
                        raise NotImplementedError(
                            f"The artifact loader '{type(self._loader).__name__}' does not implement 'upload' "
                            f"and no agnostical S3 fallback layout could be determined."
                        )
                finally:
                    await asyncio.to_thread(shutil.rmtree, tmpdir)

            artifact_path = await self._loader.resolve(meta.name, meta.version)
            await asyncio.to_thread(model.load, artifact_path, **kwargs)

        except Exception as exc:
            self._registry.set_status(name, ModelStatus.ERROR)
            raise ModelLoadError(name, str(exc)) from exc

        self._registry.set_status(name, ModelStatus.READY)
        logger.info("model_ready", name=meta.name, version=meta.version)

    async def load_all(self) -> None:
        """Discovers and loads all available models."""
        await asyncio.to_thread(self._import_all_model_modules_full)
        for meta in self._registry.list_models():
            try:
                await self.load_model(meta.name)
            except ModelLoadError:
                logger.exception("model_load_skipped", name=meta.name)

    def unload_model(self, name: str) -> None:
        """Unloads the specified model and triggers its cleanup method.

        Args:
            name: Name of the model to unload.
        """
        model = self._registry.get(name)
        model.cleanup()
        self._registry.set_status(name, ModelStatus.UNLOADED)
        logger.info("model_unloaded", name=name)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Executes execution on the given request payload and measures execution latency.

        Args:
            request: Prediction request containing model intent and input payload.

        Returns:
            A populated PredictionResult tracking execution telemetry.

        Raises:
            ModelNotReadyError: If the targeted model is not in a READY state.
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
        """Returns metadata for all discovered models."""
        return self._registry.list_models()

    def list_model_summaries(self) -> list[ModelSummary]:
        """Returns lightweight descriptions of all discovered models."""
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
        """Returns a mapping of model categories to model names."""
        return self._registry.categories_index()

    def model_status(self, name: str) -> ModelStatus:
        """Returns the current lifecycle status of a model.

        Args:
            name: Name of the model.
        """
        return self._registry.status(name)

    def ready_models(self) -> Sequence[str]:
        """Returns a sequence of names for models currently in a READY state."""
        return [
            meta.name
            for meta in self._registry.list_models()
            if self._registry.status(meta.name) == ModelStatus.READY
        ]
