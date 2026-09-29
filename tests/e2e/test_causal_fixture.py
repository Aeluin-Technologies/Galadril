"""Contracts for the bounded multi-identity E2E inference fixture."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fixtures.e2e_inference_model import E2EDeterministicModel
from galadril_inference.common.types import PredictionRequest


def _predict(
    model: E2EDeterministicModel,
    cohort: str,
    signal: float,
    outcome: float,
) -> dict[str, object]:
    """Returns one validated deterministic prediction for a mock upload."""
    payload = json.dumps(
        {
            "cohort": cohort,
            "signal": signal,
            "outcome": outcome,
            "decoy": cohort == "decoy",
        }
    )
    result = model.predict(
        PredictionRequest(
            model_name="e2e_deterministic",
            features={"data": payload},
        )
    )
    assert isinstance(result.prediction, dict)
    assert result.confidence == outcome
    return result.prediction


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    """Compares the fixture's four-dimensional identity evidence."""
    return sum(a * b for a, b in zip(left, right, strict=True))


def _vector(value: object) -> tuple[float, ...]:
    """Narrows a model embedding to finite scalar fixture coordinates."""
    assert isinstance(value, list)
    assert all(isinstance(component, (int, float)) for component in value)
    return tuple(float(component) for component in value)


def test_causal_fixture_separates_decoys_without_changing_the_label(
    tmp_path: Path,
) -> None:
    """Uses embeddings, not labels, to distinguish concordant identities."""
    model = E2EDeterministicModel()
    model.download(str(tmp_path))
    model.load(str(tmp_path))

    first = _predict(model, "alpha", 0.1, 0.2)
    second = _predict(model, "alpha", 0.9, 0.7)
    decoy = _predict(model, "decoy", 0.9, 0.7)
    assert first["label"] == second["label"] == decoy["label"]
    assert first["source_field"] == second["source_field"] == "alpha"
    assert decoy["source_field"] == "decoy"

    first_vector = _vector(first["embedding"])
    second_vector = _vector(second["embedding"])
    decoy_vector = _vector(decoy["embedding"])
    assert _dot(first_vector, second_vector) > 0.99
    assert _dot(first_vector, decoy_vector) == 0.0


def test_causal_fixture_rejects_inconsistent_decoy_claim(
    tmp_path: Path,
) -> None:
    """Prevents a malformed fixture from silently entering the pipeline."""
    model = E2EDeterministicModel()
    model.download(str(tmp_path))
    model.load(str(tmp_path))
    with pytest.raises(ValueError, match="decoy flag"):
        model.predict(
            PredictionRequest(
                model_name="e2e_deterministic",
                features={
                    "data": '{"cohort":"alpha","signal":0.1,'
                    '"outcome":0.2,"decoy":true}'
                },
            )
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
