import pytest
from typing import Any

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
        """Provides the metadata for the mock model.

        Returns:
            ModelMeta: Metadata containing name, version, and tags.
        """
        return ModelMeta(
            name="dummy_test", version="1.0.0", description="Dummy", tags={}
        )

    def load(self, artifact_path: str) -> None:
        """Simulates the loading of model artifacts.

        Args:
            artifact_path: The filesystem path to the model artifacts.
        """
        pass

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Simulates a model prediction.

        Args:
            request: The prediction request containing input features.

        Returns:
            PredictionResult: A static 'ok' result with full confidence.
        """
        return PredictionResult(
            model_name="dummy_test",
            model_version="1.0.0",
            prediction={"result": "ok"},
            confidence=1.0,
        )

    def input_schema(self) -> dict[str, Any]:
        """Returns the expected JSON schema for model inputs.

        Returns:
            dict[str, Any]: An empty schema for testing.
        """
        return {}

    def output_schema(self) -> dict[str, Any]:
        """Returns the expected JSON schema for model outputs.

        Returns:
            dict[str, Any]: An empty schema for testing.
        """
        return {}

    def cleanup(self) -> None:
        """Simulates resource cleanup for the model."""
        pass


def test_registry_discovery():
    """Verifies that the registry can scan and register available model classes.

    Ensures that the automatic discovery mechanism populates the internal
    registry correctly.
    """
    registry = ModelRegistry()
    count = registry.discover()

    assert count > 0
    assert "dummy_test" in registry
    assert len(registry.list_models()) == len(registry)


def test_registry_get_and_status():
    """Tests retrieval of model instances and status management within the registry.

    Ensures that models can be fetched by name and that their operational
    status is tracked correctly.
    """
    registry = ModelRegistry()
    registry.discover()

    model = registry.get("dummy_test")
    assert isinstance(model, DummyTestModel)

    assert registry.status("dummy_test") == ModelStatus.UNLOADED

    registry.set_status("dummy_test", ModelStatus.READY)
    assert registry.status("dummy_test") == ModelStatus.READY


def test_registry_not_found():
    """Verifies that the registry raises appropriate errors for missing models.

    Ensures ModelNotFoundError is raised when attempting to access or query
    the status of a model that has not been registered.
    """
    registry = ModelRegistry()

    with pytest.raises(ModelNotFoundError):
        registry.get("unknown_model")

    with pytest.raises(ModelNotFoundError):
        registry.status("unknown_model")


def test_registry_cleanup():
    """Verifies that the cleanup process resets model statuses across the registry.

    Ensures that calling cleanup_all transitions all registered models back
    to an UNLOADED state.
    """
    registry = ModelRegistry()
    registry.discover()
    registry.set_status("dummy_test", ModelStatus.READY)

    registry.cleanup_all()

    assert registry.status("dummy_test") == ModelStatus.UNLOADED
