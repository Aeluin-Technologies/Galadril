import pytest

from galadril_inference.common.exceptions import (
    ModelLoadError,
    ModelNotReadyError,
)
from galadril_inference.common.types import ModelStatus, PredictionRequest
from galadril_inference.core.engine import InferenceEngine
from galadril_inference.loading.loader import ArtifactLoader


class DummyLoader(ArtifactLoader):
    """A mock artifact loader for simulating path resolution and download errors."""

    def resolve(self, model_name: str, version: str) -> str:
        """Resolves a model name and version to a mock local path.

        Args:
            model_name: The name of the model to resolve.
            version: The semantic version string.

        Returns:
            A string representing the mock artifact path.

        Raises:
            ValueError: If the model_name is "error_model", simulating a failure.
        """
        if model_name == "error_model":
            raise ValueError("Simulated download error")
        return f"/tmp/mock/{model_name}/{version}"

    def exists(self, model_name: str, version: str) -> bool:
        """Simulates a check for the existence of model artifacts.

        Args:
            model_name: Name of the model.
            version: Version of the model.

        Returns:
            True, assuming the artifacts always exist in this mock context.
        """
        return True


@pytest.fixture
def engine():
    """Provides a fresh InferenceEngine instance for each test case.

    Returns:
        InferenceEngine: An engine instance configured with the DummyLoader.
    """
    return InferenceEngine(loader=DummyLoader())


def test_engine_initialization(engine):
    """Verifies that the engine discovers models correctly upon initialization.

    Args:
        engine: The InferenceEngine fixture.
    """
    models = engine.list_models()
    assert len(models) > 0
    assert any(m.name == "dummy_test" for m in models)


def test_engine_load_and_unload(engine):
    """Tests the full loading and unloading lifecycle of a model.

    Ensures the internal state transitions from UNLOADED to READY and back.

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
    """Tests the prediction workflow and requirement for models to be READY.

    Validates that predictions fail when a model is not loaded and succeed
    when it is, including verification of performance metrics.

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
    """Tests engine error handling when a model fails to load.

    Uses monkeypatching to simulate a runtime failure during the model's
    internal loading process and verifies the engine sets an ERROR status.

    Args:
        engine: The InferenceEngine fixture.
    """
    model = engine._registry.get("dummy_test")

    def failing_load(path):
        """Mock load function that always raises a RuntimeError."""
        raise RuntimeError("Load crash")

    model.load = failing_load

    with pytest.raises(ModelLoadError):
        engine.load_model("dummy_test")

    assert engine.model_status("dummy_test") == ModelStatus.ERROR
