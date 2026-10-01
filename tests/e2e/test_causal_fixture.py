"""Contracts for the bounded multi-identity E2E inference fixture."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import networkx as nx
import pandas as pd
import pytest
from amarth import Observation, ObservationWindow
from amarth.estimation.dowhy import DowhyEstimator
from amarth.router import AmarthRouter
from empirical_data import load_retail_rows, load_trial_rows, trial_estimate
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


def test_retail_identity_model_keeps_source_customer_evidence_distinct(
    tmp_path: Path,
) -> None:
    """Rows from one source customer merge without merging a lookalike."""
    model = E2EDeterministicModel()
    model.download(str(tmp_path))
    model.load(str(tmp_path))

    def predict(customer_id: int) -> dict[str, object]:
        result = model.predict(
            PredictionRequest(
                model_name="e2e_deterministic",
                features={
                    "data": json.dumps(
                        {"customer_id": customer_id, "source_row": 1}
                    )
                },
            )
        )
        assert isinstance(result.prediction, dict)
        return result.prediction

    first = predict(17850)
    same = predict(17850)
    lookalike = predict(13047)
    assert first["scalar_evidence"] == {"source_row": 1}
    assert first["source_field"] == same["source_field"]
    assert first["source_field"] != lookalike["source_field"]
    assert _dot(_vector(first["embedding"]), _vector(same["embedding"])) == 1.0
    assert (
        _dot(_vector(first["embedding"]), _vector(lookalike["embedding"])) < 0.1
    )


def test_all_empirical_customer_vectors_are_separated(tmp_path: Path) -> None:
    """The pinned 24-customer oracle cannot collapse from vector collision."""
    model = E2EDeterministicModel()
    model.download(str(tmp_path))
    model.load(str(tmp_path))
    vectors = []
    for customer_id in sorted({row.customer_id for row in load_retail_rows()}):
        prediction = model.predict(
            PredictionRequest(
                model_name="e2e_deterministic",
                features={
                    "data": json.dumps(
                        {"customer_id": customer_id, "source_row": 1}
                    )
                },
            )
        ).prediction
        vectors.append(_vector(prediction["embedding"]))
    assert len(vectors) == 24
    assert all(
        _dot(left, right) < 0.85
        for index, left in enumerate(vectors)
        for right in vectors[index + 1 :]
    )


def test_randomized_trial_model_preserves_assignment_and_outcome(
    tmp_path: Path,
) -> None:
    """The deterministic model must not infer or rewrite trial outcomes."""
    model = E2EDeterministicModel()
    model.download(str(tmp_path))
    model.load(str(tmp_path))
    result = model.predict(
        PredictionRequest(
            model_name="e2e_deterministic",
            features={
                "data": json.dumps(
                    {"record_id": 7, "condition": 6, "units_selected": 3.5}
                )
            },
        )
    )
    assert result.prediction["scalar_evidence"] == {
        "treatment": 0.0,
        "outcome": 3.5,
    }


def test_amarth_estimates_randomized_trial_without_claiming_significance() -> (
    None
):
    """Uses the declared randomization DAG, not a mined correlation edge."""
    rows = load_trial_rows()
    frame = pd.DataFrame(
        {
            "treatment": [1 if row.condition == 1 else 0 for row in rows],
            "outcome": [row.units_selected for row in rows],
        }
    )
    graph = nx.DiGraph([("treatment", "outcome")])
    result = DowhyEstimator(refutation_simulations=0).estimate_effect(
        frame, graph, "treatment", "outcome"
    )
    assert result is not None
    assert result.ate == pytest.approx(
        trial_estimate(rows).difference, abs=0.01
    )
    assert trial_estimate(rows).lower < 0 < trial_estimate(rows).upper


@pytest.mark.parametrize(
    ("base_seconds", "extra_every", "extra_seconds"),
    [(1, 0, 0), (1, 3, 1), (1, 2, 2), (1, 3, 3), (2, 4, 2)],
)
@pytest.mark.parametrize("window_phase_seconds", [0, 40, 80])
def test_causal_fixture_produces_discoverable_lag(
    tmp_path: Path,
    base_seconds: int,
    extra_every: int,
    extra_seconds: int,
    window_phase_seconds: int,
) -> None:
    """Checks that the real causal analyzer can recover the fixture's lag."""
    model = E2EDeterministicModel()
    model.download(str(tmp_path))
    model.load(str(tmp_path))
    start = datetime(2026, 9, 30, tzinfo=UTC) + timedelta(
        seconds=window_phase_seconds
    )
    observations: list[Observation] = []
    elapsed = 10
    for index in range(30):
        signal = (((index * 7) % 13) + 1) / 14.0
        outcome = ((((index - 1) * 7) % 13) + 1) / 14.0
        prediction = _predict(model, "alpha", signal, outcome)
        observed_at = start + timedelta(seconds=elapsed)
        observations.extend(
            (
                Observation(
                    observation_id=f"state:{index}",
                    graph_node_id="alpha",
                    observed_at=observed_at,
                    observation_type="E2E_OBSERVATION",
                    scalar_values={"confidence": outcome},
                ),
                Observation(
                    observation_id=f"embedding:{index}",
                    graph_node_id="alpha",
                    observed_at=observed_at,
                    observation_type="E2E_OBSERVATION",
                    embeddings={
                        "e2e_embedding": _vector(prediction["embedding"])
                        + (0.0,) * 1020
                    },
                ),
                Observation(
                    observation_id=f"event:{index}",
                    graph_node_id=f"event-{index}",
                    observed_at=observed_at,
                    observation_type="Observation",
                    scalar_values={"presence": 1.0},
                ),
            )
        )
        elapsed += base_seconds
        if extra_every and index % extra_every == 0:
            elapsed += extra_seconds
    window = ObservationWindow(
        start=start,
        end=start + timedelta(seconds=120),
        bucket=timedelta(seconds=1),
        observations=tuple(observations),
    )

    result = AmarthRouter(strict_dag=True).analyze_observation_window(
        window,
        "E2E_OBSERVATION.confidence",
        analysis_window_size="120s",
    )

    assert any(
        link.source_feature.startswith("E2E_OBSERVATION.e2e_embedding.pc")
        and link.target_feature == "E2E_OBSERVATION.confidence"
        for link in result["causal_links"]
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
