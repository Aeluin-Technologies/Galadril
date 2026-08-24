"""Branch-focused tests for causal analysis orchestration."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import networkx as nx
import numpy as np
import pandas as pd
import pytest
from amarth import Observation, ObservationWindow
from amarth.router import AmarthRouter


def test_analyze_observation_window_delegates_prepared_contract() -> None:
    """Passes aligned ESKG features and provenance to the shared analyzer."""
    start = datetime(2026, 8, 24, tzinfo=UTC)
    window = ObservationWindow(
        start=start,
        end=start + timedelta(seconds=1),
        bucket=timedelta(seconds=1),
        observations=(
            Observation(
                observation_id="one",
                graph_node_id="node-one",
                observed_at=start,
                observation_type="Signal",
                scalar_values={"value": 1.0},
            ),
        ),
    )
    router = AmarthRouter()
    with patch.object(router, "analyze", return_value={"ok": True}) as analyze:
        result = router.analyze_observation_window(
            window,
            "Signal.value",
            analysis_window_size="2s",
        )

    assert result == {"ok": True}
    assert analyze.call_args.kwargs["time_col"] == "timestamp"
    assert analyze.call_args.kwargs["window_size"] == "2s"
    assert analyze.call_args.kwargs["sampling_interval_seconds"] == 1.0


def test_analyze_static_data_returns_complete_metadata() -> None:
    """Runs the static path and preserves legacy single-embedding input."""
    frame = pd.DataFrame(
        {
            "embedding": [[0.1, 0.2], [0.2, 0.3], [0.3, 0.4]],
            "outcome": [1.0, 2.0, 3.0],
        }
    )
    dag = nx.DiGraph([("embedding.pc0", "outcome")])
    router = AmarthRouter()
    with (
        patch.object(router, "_static_discovery", return_value=dag),
        patch.object(router, "_estimate_effects", return_value=["effect"]),
    ):
        result = router.analyze(
            frame,
            "outcome",
            embedding_col="embedding",
            sampling_interval_seconds=2.0,
        )

    assert result["causal_effects"] == ["effect"]
    assert result["metadata"]["is_temporal"] is False
    assert result["metadata"]["used_embeddings"] is True
    assert result["metadata"]["sampling_interval_seconds"] == 2.0
    assert "embedding.pc0" in result["analysis_frame"]


def test_analyze_temporal_data_aligns_and_infers_interval() -> None:
    """Runs temporal discovery and converts lag steps into elapsed seconds."""
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=12, freq="3s"),
            "cause": np.arange(12, dtype=float),
            "outcome": np.arange(12, dtype=float) * 2.0,
        }
    )
    dag = nx.DiGraph()
    dag.add_edge(
        "cause",
        "outcome",
        optimal_lag=2,
        confidence_score=0.8,
        effect_size=0.5,
    )
    router = AmarthRouter()
    with (
        patch.object(router, "_windowed_temporal_discovery", return_value=dag),
        patch.object(router, "_estimate_effects", return_value=[]),
    ):
        result = router.analyze(frame, "outcome", time_col="timestamp")

    assert result["metadata"]["is_temporal"] is True
    assert result["metadata"]["samples_processed"] == 10
    assert result["metadata"]["sampling_interval_seconds"] == 3.0
    assert result["causal_links"][0].time_lag_seconds == 6.0


@pytest.mark.parametrize(
    ("duration", "expected_window", "expected_tau"),
    [
        (timedelta(hours=1), "1h", 5),
        (timedelta(days=2), "1D", 5),
        (timedelta(days=60), "30D", 5),
        (timedelta(days=500), "365D", 5),
    ],
)
def test_dynamic_parameters_follow_observation_horizon(
    duration: timedelta, expected_window: str, expected_tau: int
) -> None:
    """Selects bounded PCMCI parameters across supported temporal horizons."""
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2025-01-01", periods=101, freq=duration / 100
            )
        }
    )
    params = AmarthRouter()._infer_dynamic_parameters(frame, "timestamp")

    assert params["window_size"] == expected_window
    assert params["tau_max"] == expected_tau


def test_dynamic_parameters_handle_static_and_invalid_window() -> None:
    """Provides safe defaults for static data and malformed window strings."""
    frame = pd.DataFrame(
        {"timestamp": pd.date_range("2026-01-01", periods=6, freq="1h")}
    )
    static = AmarthRouter()._infer_dynamic_parameters(frame, None)
    invalid = AmarthRouter()._infer_dynamic_parameters(
        frame, "timestamp", user_window_size="invalid"
    )

    assert static == {
        "window_size": None,
        "tau_max": 0,
        "stability": 0.5,
        "min_window_samples": 2,
    }
    assert invalid["window_size"] == "invalid"
    assert invalid["stability"] == 1.0


def test_preprocess_imputes_scalar_and_vector_data() -> None:
    """Uses temporal interpolation and static medians without losing vectors."""
    frame = pd.DataFrame(
        {
            "value": [1.0, np.nan, 3.0],
            "empty": [np.nan, np.nan, np.nan],
            "vector": [[1.0], None, [3.0]],
        }
    )
    router = AmarthRouter()
    temporal = router._preprocess_data(frame, is_temporal=True)
    static = router._preprocess_data(frame, is_temporal=False)

    assert temporal["value"].tolist() == [1.0, 2.0, 3.0]
    assert static["value"].tolist() == [1.0, 2.0, 3.0]
    assert "empty" not in temporal
    assert temporal["vector"].iloc[1] == [1.0]


def test_expand_embeddings_handles_missing_empty_full_and_reduced_vectors() -> (
    None
):
    """Covers embedding omission, scalar removal, direct expansion, and PCA."""
    router = AmarthRouter()
    frame = pd.DataFrame(
        {
            "scalar_only": [1.0, 2.0],
            "small": [[1.0], [2.0]],
        }
    )
    expanded = router._expand_embeddings(
        frame, ("missing", "scalar_only", "small")
    )
    assert "scalar_only" not in expanded
    assert expanded["small.pc0"].tolist() == [1.0, 2.0]

    large = pd.DataFrame(
        {"embedding": [np.arange(4, dtype=float) + row for row in range(20)]}
    )
    reduced = router._expand_embeddings(large, ("embedding",))
    assert list(reduced.columns) == ["embedding.pc0", "embedding.pc1"]


def test_expand_embeddings_rejects_inconsistent_dimensions() -> None:
    """Rejects vector shape drift before causal discovery."""
    frame = pd.DataFrame({"embedding": [[1.0, 2.0], [3.0]]})
    with pytest.raises(ValueError, match="inconsistent dimensions"):
        AmarthRouter()._expand_embeddings(frame, ("embedding",))


def test_windowed_discovery_uses_resampling_fallback_and_prior_support() -> (
    None
):
    """Aggregates eligible windows and annotates ontology-supported edges."""
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=30, freq="1min"),
            "x": np.arange(30, dtype=float),
            "y": np.arange(30, dtype=float),
        }
    )
    discovered = nx.DiGraph()
    discovered.add_edge("x", "y", confidence_score=0.8, optimal_lag=1)
    prior = nx.DiGraph([("x", "y")])
    with patch(
        "amarth.router.discover_graph", return_value=discovered
    ) as discover:
        consensus = AmarthRouter()._windowed_temporal_discovery(
            frame, "timestamp", "1min", 1, 0.5, prior, "y"
        )

    discover.assert_called_once()
    assert consensus["x"]["y"]["prior_supported"] is True


def test_pcmci_budget_prioritizes_target_neighborhood_and_limits_windows() -> (
    None
):
    """Bounds PCMCI variables and windows while retaining ESKG-local causes."""
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=90, freq="1min"),
            "target": np.arange(90, dtype=float),
            "prior_cause": np.arange(90, dtype=float),
            "filler_one": np.arange(90, dtype=float),
            "filler_two": np.arange(90, dtype=float),
        }
    )
    prior = nx.DiGraph([("prior_cause", "target")])
    with patch(
        "amarth.router.discover_graph", return_value=nx.DiGraph()
    ) as discover:
        AmarthRouter(
            max_pcmci_variables=3, max_analysis_windows=2
        )._windowed_temporal_discovery(
            frame,
            "timestamp",
            "30min",
            1,
            0.5,
            prior,
            "target",
        )

    assert discover.call_count == 2
    for call in discover.call_args_list:
        assert list(call.args[0].columns) == [
            "target",
            "prior_cause",
            "filler_one",
        ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_pcmci_variables": 1}, "max_pcmci_variables"),
        ({"max_analysis_windows": 0}, "max_analysis_windows"),
    ],
)
def test_router_rejects_invalid_computation_budgets(
    kwargs: dict[str, int], message: str
) -> None:
    """Rejects settings that make causal discovery invalid or unbounded."""
    with pytest.raises(ValueError, match=message):
        AmarthRouter(**kwargs)


def test_static_discovery_uses_only_prior_graph_evidence() -> None:
    """Filters ontology evidence to observable scalar variables."""
    frame = pd.DataFrame({"x": [1.0], "y": [2.0], "label": ["a"]})
    prior = nx.DiGraph()
    prior.add_edge("x", "y", confidence_score=0.8)
    prior.add_edge("x", "missing")

    graph = AmarthRouter()._static_discovery(frame, prior)

    assert list(graph.edges) == [("x", "y")]
    assert graph["x"]["y"]["prior_supported"] is True
    assert graph["x"]["y"]["method"] == "eskg.prior"


def test_static_discovery_requires_prior_graph() -> None:
    """Refuses causal orientation when neither time nor ontology is available."""
    frame = pd.DataFrame({"x": [1.0], "y": [2.0]})
    with pytest.raises(ValueError, match="requires prior_graph"):
        AmarthRouter()._static_discovery(frame, None)


def test_aggregate_dags_calibrates_statistics_and_breaks_cycles() -> None:
    """Uses median evidence, stability filtering, and confidence cycle pruning."""
    first = nx.DiGraph()
    first.add_edge(
        "a",
        "b",
        optimal_lag=1,
        effect_size=0.6,
        p_value=0.01,
        q_value=0.02,
        confidence_score=0.8,
        method="pcmci",
    )
    first.add_edge("b", "a", effect_size=0.1, confidence_score=0.2)
    first.add_edge("rare", "edge", confidence_score=0.9)
    second = nx.DiGraph()
    second.add_edge(
        "a",
        "b",
        optimal_lag=3,
        effect_size=1.0,
        p_value=0.03,
        q_value=0.04,
        confidence_score=0.6,
    )
    second.add_edge("b", "a", effect_size=0.1, confidence_score=0.2)

    consensus = AmarthRouter(strict_dag=True)._aggregate_dags(
        [first, second], 0.75
    )

    assert consensus.has_edge("a", "b")
    assert not consensus.has_edge("b", "a")
    assert not consensus.has_edge("rare", "edge")
    assert consensus["a"]["b"]["optimal_lag"] == 2
    assert consensus["a"]["b"]["effect_size"] == pytest.approx(0.8)
    assert consensus["a"]["b"]["p_value"] == pytest.approx(0.02)


def test_aggregate_dags_handles_empty_and_missing_statistics() -> None:
    """Defaults missing optional evidence and permits cycles when configured."""
    router = AmarthRouter(strict_dag=False)
    assert len(router._aggregate_dags([], 0.5)) == 0
    dag = nx.DiGraph([("a", "b"), ("b", "a")])
    consensus = router._aggregate_dags([dag], 0.5)

    assert set(consensus.edges) == {("a", "b"), ("b", "a")}
    assert consensus["a"]["b"]["optimal_lag"] == 0
    assert consensus["a"]["b"]["p_value"] is None
    assert consensus["a"]["b"]["confidence_score"] == 1.0


def test_align_temporal_data_handles_missing_zero_and_positive_lags() -> None:
    """Shifts only positive-lag antecedents and removes incomplete leading rows."""
    frame = pd.DataFrame(
        {"cause": [1, 2, 3], "zero": [4, 5, 6], "out": [7, 8, 9]}
    )
    router = AmarthRouter()
    assert router._align_temporal_data(frame, nx.DiGraph(), "out") is frame
    dag = nx.DiGraph()
    dag.add_edge("cause", "out", optimal_lag=1)
    dag.add_edge("zero", "out", optimal_lag=0)
    aligned = router._align_temporal_data(frame, dag, "out")

    assert len(aligned) == 2
    assert aligned["cause"].tolist() == [1.0, 2.0]
    assert aligned["zero"].tolist() == [5, 6]


def test_estimate_effects_filters_failures_and_failed_refutations() -> None:
    """Retains only estimable, refutation-passing direct antecedents."""
    dag = nx.DiGraph(
        [("good", "out"), ("none", "out"), ("failed", "out"), ("error", "out")]
    )
    frame = pd.DataFrame(
        {"good": [1], "none": [1], "failed": [1], "error": [1], "out": [1]}
    )

    def estimate(*, treatment: str, **_kwargs: object) -> object | None:
        if treatment == "error":
            raise ValueError("bad estimate")
        if treatment == "none":
            return None
        return SimpleNamespace(refutation_passed=treatment == "good")

    estimator = MagicMock()
    estimator.estimate_effect.side_effect = estimate
    with patch("amarth.router.DowhyEstimator", return_value=estimator):
        results = AmarthRouter()._estimate_effects(frame, dag, "out")

    assert len(results) == 1
    assert results[0].refutation_passed is True
    assert AmarthRouter()._estimate_effects(frame, dag, "absent") == []


def test_causal_link_defensively_normalizes_optional_attributes() -> None:
    """Clamps probabilities and defaults malformed optional graph attributes."""
    dag = nx.DiGraph()
    dag.add_edge(
        "source.pc0",
        "target",
        confidence_score=2.0,
        stability=-1.0,
        p_value=2.0,
        q_value=np.nan,
        weight=-0.5,
    )
    link = AmarthRouter()._build_causal_links(
        dag,
        sampling_interval_seconds=1.0,
        feature_node_ids={"source": ("node",)},
    )[0]

    assert link.confidence_score == 1.0
    assert link.stability == 0.0
    assert link.p_value == 1.0
    assert link.q_value is None
    assert link.effect_size == -0.5
    assert link.source_node_ids == ("node",)


def test_target_resolution_interval_and_cycle_helpers() -> None:
    """Covers exact, derived, ambiguous target, cadence, and cycle utilities."""
    router = AmarthRouter()
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="4s"),
            "exact": [1, 2],
            "derived.value": [1, 2],
            "embedding.pc0": [1, 2],
        }
    )
    assert router._resolve_target(frame, "exact") == "exact"
    assert router._resolve_target(frame, "derived") == "derived.value"
    with pytest.raises(ValueError, match="absent or ambiguous"):
        router._resolve_target(
            frame.assign(**{"derived.other": [1, 2]}), "derived"
        )
    assert router._infer_interval(frame, "timestamp") == 4.0
    assert router._infer_interval(frame.iloc[:1], "timestamp") == 0.0
    assert router._infer_interval(frame, None) == 0.0

    cycle = nx.DiGraph()
    cycle.add_edge("a", "b", confidence_score=0.9)
    cycle.add_edge("b", "a", confidence_score=0.1)
    clean = router._break_cycles(cycle)
    assert list(clean.edges) == [("a", "b")]
