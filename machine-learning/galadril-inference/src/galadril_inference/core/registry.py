"""Model registry with automatic discovery."""

from __future__ import annotations

import structlog

from galadril_inference.common.exceptions import ModelNotFoundError
from galadril_inference.common.types import ModelMeta, ModelStatus
from galadril_inference.models.base import BaseModel

logger = structlog.get_logger(__name__)


class ModelRegistry:
    """Discovers, stores, and manages the lifecycle of inference models."""

    def __init__(self) -> None:
        self._models: dict[str, BaseModel] = {}
        self._status: dict[str, ModelStatus] = {}
        self._categories: dict[str, str] = {}

    def discover(self) -> int:
        """Scan all concrete BaseModel subclasses and register them."""
        discovered = 0

        for model_cls in self._all_concrete_subclasses(BaseModel):
            instance = model_cls()
            meta = instance.meta()

            if meta.name in self._models:
                logger.debug(
                    "model_already_registered",
                    name=meta.name,
                    class_name=model_cls.__name__,
                )
                continue

            self._models[meta.name] = instance
            self._status[meta.name] = ModelStatus.UNLOADED
            self._categories[meta.name] = self._infer_category(model_cls)
            discovered += 1
            logger.info(
                "model_discovered",
                name=meta.name,
                version=meta.version,
                class_name=model_cls.__name__,
                category=self._categories[meta.name],
            )

        return discovered

    def get(self, name: str) -> BaseModel:
        """Return a model instance by name.

        Raises:
            ModelNotFoundError: if no model with that name is registered.
        """
        try:
            return self._models[name]
        except KeyError:
            raise ModelNotFoundError(
                f"Model '{name}' not found. "
                f"Available: {sorted(self._models.keys())}"
            ) from None

    def list_models(self) -> list[ModelMeta]:
        """Return metadata for every registered model."""
        return [m.meta() for m in self._models.values()]

    def category(self, name: str) -> str:
        """Return the inferred category for a model name.

        Models defined directly in :mod:`galadril_inference.models` (no
        subpackage) are categorized as "uncategorized".
        """
        if name not in self._categories:
            raise ModelNotFoundError(f"Model '{name}' is not registered.")
        return self._categories[name]

    def categories_index(self) -> dict[str, list[str]]:
        """Return a stable index of category -> sorted model names."""
        index: dict[str, list[str]] = {}
        for name, cat in self._categories.items():
            index.setdefault(cat, []).append(name)

        for names in index.values():
            names.sort()

        return dict(sorted(index.items(), key=lambda kv: kv[0]))

    def __contains__(self, name: str) -> bool:
        return name in self._models

    def __len__(self) -> int:
        return len(self._models)

    def status(self, name: str) -> ModelStatus:
        """Return the current lifecycle status of a model."""
        if name not in self._status:
            raise ModelNotFoundError(f"Model '{name}' is not registered.")
        return self._status[name]

    def set_status(self, name: str, status: ModelStatus) -> None:
        """Update the lifecycle status of a model."""
        if name not in self._models:
            raise ModelNotFoundError(f"Model '{name}' is not registered.")
        previous = self._status[name]
        self._status[name] = status
        logger.debug(
            "model_status_changed",
            name=name,
            old_status=previous.value,
            new_status=status.value,
        )

    def cleanup_all(self) -> None:
        """Call cleanup() on every registered model."""
        for name, model in self._models.items():
            try:
                model.cleanup()
                self._status[name] = ModelStatus.UNLOADED
                logger.info("model_cleaned_up", name=name)
            except Exception:
                logger.exception("model_cleanup_failed", name=name)

    @staticmethod
    def _infer_category(model_cls: type[BaseModel]) -> str:
        module = getattr(model_cls, "__module__", "")
        prefix = "galadril_inference.models."
        if not module.startswith(prefix):
            return "uncategorized"

        remainder = module[len(prefix) :]
        if not remainder or "." not in remainder:
            return "uncategorized"

        return remainder.split(".", 1)[0] or "uncategorized"

    @staticmethod
    def _all_concrete_subclasses(cls: type) -> list[type]:
        """Walk the subclass tree and return only concrete (non-abstract) classes.

        Returns a sorted list (by class name) for deterministic discovery order.
        """
        result: list[type] = []
        work = list(cls.__subclasses__())

        while work:
            child = work.pop()
            work.extend(child.__subclasses__())
            # Skip classes that still have unimplemented abstract methods.
            if not getattr(child, "__abstractmethods__", set()):
                result.append(child)

        return sorted(result, key=lambda c: c.__name__)
