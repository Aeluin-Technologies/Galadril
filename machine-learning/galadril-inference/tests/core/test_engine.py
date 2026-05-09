import pytest

from galadril_inference.common.exceptions import (
    ModelLoadError,
    ModelNotReadyError,
)
from galadril_inference.common.types import ModelStatus, PredictionRequest
from galadril_inference.core.engine import InferenceEngine
from galadril_inference.loading.loader import ArtifactLoader


class DummyLoader(ArtifactLoader):
    """A mock loader that returns static paths or simulates download errors."""

    def resolve(self, model_name: str, version: str) -> str:
        """Simulates the resolution of a model artifact path.

        Args:
            model_name: The unique identifier of the model.
            version: The version string of the model.

        Returns:
            A mock filesystem path string.

        Raises:
            ValueError: If model_name is "error_model", simulating a download failure.
        """
        if model_name == "error_model":
            raise ValueError("Simulated download error")
        return f"/tmp/mock/{model_name}/{version}"

    def exists(self, model_name: str, version: str) -> bool:
        """Checks if the model artifact exists.

        Args:
            model_name: The name of the model to check.
            version: The version of the model.

        Returns:
            True, as this mock assumes all requested models exist.
        """
        return True


@pytest.fixture
def engine():
    """Initializes an InferenceEngine with a DummyLoader for testing.

    Returns:
        InferenceEngine: An instance of the engine configured with mock loading logic.
    """
    return InferenceEngine(loader=DummyLoader())


def test_engine_initialization(engine):
    """Verifies that the engine correctly discovers registered models on startup.

    Args:
        engine: The InferenceEngine fixture.
    """
    models = engine.list_models()
    assert len(models) > 0
    assert any(m.name == "dummy_test" for m in models)


def test_engine_load_and_unload(engine):
    """Tests the state transitions of a model during loading and unloading.

    Ensures that the model status moves correctly between UNLOADED and READY.

    Args:
        engine: The InferenceEngine fixture.
    """
    assert engine.model_status("dummy_test") == ModelStatus.UNLOADED

    engine.load_model("dummy_test")
    assert engine.model_status("dummy_test") == ModelStatus.READY
    assert "dummy_test" in engine.ready_models()

    engine.unload_model("dummy_test")
    assert engine.model_status("dummy_test") == ModelStatus.UNLOADED
    assert "dummy_test" not in engine.ready_models()


def test_engine_predict_lifecycle(engine):
    """Tests the full prediction lifecycle, including error handling for unloaded models.

    Args:
        engine: The InferenceEngine fixture.
    """
    req = PredictionRequest(model_name="dummy_test", features={})

    with pytest.raises(ModelNotReadyError):
        engine.predict(req)

    engine.load_model("dummy_test")

    result = engine.predict(req)
    assert result.model_name == "dummy_test"
    assert result.prediction == {"result": "ok"}
    assert result.latency_ms is not None


def test_engine_load_error(engine):
    """Tests engine behavior when a model fails to load into memory.

    This test mocks a runtime failure during the model's internal load process
    to ensure the engine transitions to an ERROR state.

    Args:
        engine: The InferenceEngine fixture.
    """
    model = engine._registry.get("dummy_test")

    def failing_load(path):
        """Mock function to simulate a crash during model artifact loading."""
        raise RuntimeError("Load crash")

    model.load = failing_load

    with pytest.raises(ModelLoadError):
        engine.load_model("dummy_test")

    assert engine.model_status("dummy_test") == ModelStatus.ERROR
