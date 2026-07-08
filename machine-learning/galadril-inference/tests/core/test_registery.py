"""Unit tests for the model registry."""

from __future__ import annotations

from typing import Any

import pytest
from galadril_inference.common.exceptions import ModelNotFoundError
from galadril_inference.common.types import (
    ModelMeta,
    ModelStatus,
    PredictionRequest,
    PredictionResult,
)
from galadril_inference.core.registry import ModelRegistry
from galadril_inference.models.base import BaseModel


class DummyTestModel(BaseModel):
    """A mock model implementation for testing registry logic without ML overhead."""

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name="dummy_test",
            version="1.0.0",
            description="Dummy",
            tags={},
        )

    def load(self, artifact_path: str) -> None:
        return None

    def predict(self, request: PredictionRequest) -> PredictionResult:
        return PredictionResult(
            model_name="dummy_test",
            model_version="1.0.0",
            prediction={"result": "ok"},
            confidence=1.0,
        )

    def input_schema(self) -> dict[str, Any]:
        return {}

    def output_schema(self) -> dict[str, Any]:
        return {}

    def cleanup(self) -> None:
        return None


def test_registry_discovery() -> None:
    """Verifies that the registry can scan and register available model classes.

    Note: discovery only finds BaseModel subclasses that are already imported.
    Production code ensures this via InferenceEngine module auto-import.
    """
    registry = ModelRegistry()
    count = registry.discover()

    assert count > 0
    assert len(registry.list_models()) == len(registry)


def test_registry_get_and_status() -> None:
    """Tests retrieval of model instances and status management within the registry."""
    registry = ModelRegistry()
    registry.discover()

    models = registry.list_models()
    assert models  # Non-empty, discover() returned > 0 earlier.

    first_name = models[0].name
    assert registry.status(first_name) == ModelStatus.UNLOADED

    registry.set_status(first_name, ModelStatus.READY)
    assert registry.status(first_name) == ModelStatus.READY


def test_registry_not_found() -> None:
    """Verifies that missing models raise ModelNotFoundError."""
    registry = ModelRegistry()

    with pytest.raises(ModelNotFoundError):
        registry.get("unknown_model")

    with pytest.raises(ModelNotFoundError):
        registry.status("unknown_model")


def test_registry_cleanup_resets_statuses() -> None:
    """Verifies that cleanup_all() resets model statuses across the registry."""
    registry = ModelRegistry()
    registry.discover()

    models = registry.list_models()
    assert models

    name = models[0].name
    registry.set_status(name, ModelStatus.READY)

    registry.cleanup_all()

    assert registry.status(name) == ModelStatus.UNLOADED


def test_infer_category_uncategorized_for_top_level_module() -> None:
    """Models defined directly under galadril_inference.models.* are uncategorized."""

    class TopLevel(BaseModel):
        __module__ = "galadril_inference.models.top_level"

        def meta(self) -> ModelMeta:
            return ModelMeta(name="top", version="0.0.1")

        def load(self, artifact_path: str) -> None:
            return None

        def predict(self, request: PredictionRequest) -> PredictionResult:
            raise AssertionError("Not used in this test.")

        def input_schema(self) -> dict[str, Any]:
            return {}

        def output_schema(self) -> dict[str, Any]:
            return {}

        def cleanup(self) -> None:
            return None

    assert ModelRegistry._infer_category(TopLevel) == "uncategorized"


def test_infer_category_from_subpackage_module() -> None:
    """Models defined under galadril_inference.models.<category>.* infer that category."""

    class Embedding(BaseModel):
        __module__ = "galadril_inference.models.embedding.bgem3"

        def meta(self) -> ModelMeta:
            return ModelMeta(name="emb", version="0.0.1")

        def load(self, artifact_path: str) -> None:
            return None

        def predict(self, request: PredictionRequest) -> PredictionResult:
            raise AssertionError("Not used in this test.")

        def input_schema(self) -> dict[str, Any]:
            return {}

        def output_schema(self) -> dict[str, Any]:
            return {}

        def cleanup(self) -> None:
            return None

    assert ModelRegistry._infer_category(Embedding) == "embedding"


def test_categories_index_sorted_and_stable() -> None:
    """categories_index() returns deterministic ordering and sorted model names."""
    registry = ModelRegistry()

    registry._categories = {
        "b": "temporal",
        "a": "temporal",
        "x": "uncategorized",
    }

    assert registry.categories_index() == {
        "temporal": ["a", "b"],
        "uncategorized": ["x"],
    }


def test_modelmeta_deprecated_default_false() -> None:
    """ModelMeta.deprecated defaults to False for backward compatibility."""
    meta = ModelMeta(name="m", version="1.0.0")
    assert meta.deprecated is False


def test_modelmeta_deprecated_true_roundtrip() -> None:
    """ModelMeta.deprecated can be explicitly enabled."""
    meta = ModelMeta(name="m", version="1.0.0", deprecated=True)
    assert meta.deprecated is True
