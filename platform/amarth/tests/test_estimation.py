"""Unit tests for DoWhy and heterogeneous-effect estimation adapters."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import lightgbm as lgb
import networkx as nx
import numpy as np
import pandas as pd
import pytest
from amarth.estimation.dowhy import DowhyEstimator
from amarth.estimation.heterogeneous import EmbeddingConfounderEstimator


@pytest.fixture
def scalar_frame() -> pd.DataFrame:
    """Provides scalar treatment, mediator, and outcome observations."""
    return pd.DataFrame(
        {
            "treatment": [0.0, 1.0, 2.0],
            "mediator": [0.5, 1.5, 2.5],
            "outcome": [1.0, 2.0, 4.0],
        }
    )


def test_dowhy_estimates_direct_effect_with_statistics(
    scalar_frame: pd.DataFrame,
) -> None:
    """Maps DoWhy statistics and refutation results into the stable result API."""
    dag = nx.DiGraph()
    dag.add_edge("treatment", "outcome", status="confirmed", weight=0.8)
    get_confidence_intervals = MagicMock(return_value=(2.1, 2.9))
    estimate = SimpleNamespace(
        value=2.5,
        p_value=0.01,
        stderr=0.2,
        get_confidence_intervals=get_confidence_intervals,
        test_stat_significance=lambda: True,
        __str__=lambda self: "estimate",
    )
    model = MagicMock()
    model.identify_effect.return_value = "estimand"
    model.estimate_effect.return_value = estimate
    model.refute_estimate.return_value = SimpleNamespace(new_effect=2.4)
    with patch("amarth.estimation.dowhy.CausalModel", return_value=model):
        result = DowhyEstimator(
            confidence_interval_simulations=5
        ).estimate_effect(scalar_frame, dag, "treatment", "outcome")

    assert result is not None
    assert result.ate == pytest.approx(2.5)
    assert result.edge_status == "confirmed"
    assert result.is_significant is True
    assert result.refutation_passed is True
    assert result.p_value == pytest.approx(0.01)
    assert result.stderr == pytest.approx(0.2)
    assert (result.ci_lower, result.ci_upper) == pytest.approx((2.1, 2.9))
    model.refute_estimate.assert_called_once_with(
        "estimand",
        estimate,
        method_name="random_common_cause",
        num_simulations=5,
        random_state=0,
        n_jobs=1,
    )
    get_confidence_intervals.assert_called_once_with(
        method="bootstrap", num_simulations=5
    )


def test_dowhy_handles_indirect_and_unavailable_statistics(
    scalar_frame: pd.DataFrame,
) -> None:
    """Defaults safely when an indirect estimate exposes no usable statistics."""
    dag = nx.DiGraph([("treatment", "mediator"), ("mediator", "outcome")])

    class Estimate:
        """Represents a minimal third-party estimate object."""

        value = 1.25
        p_value = "unavailable"
        stderr = object()

        @staticmethod
        def get_confidence_intervals(**_kwargs: object) -> list[str]:
            """Returns an invalid interval to exercise defensive parsing."""
            return ["low", "high"]

        def __str__(self) -> str:
            """Returns the third-party summary."""
            return "minimal estimate"

    model = MagicMock()
    model.identify_effect.return_value = "estimand"
    model.estimate_effect.return_value = Estimate()
    model.refute_estimate.return_value = SimpleNamespace(new_effect=None)
    with patch("amarth.estimation.dowhy.CausalModel", return_value=model):
        result = DowhyEstimator(
            strict_dag=False, confidence_interval_simulations=5
        ).estimate_effect(
            scalar_frame, dag, "treatment", "outcome", method_name="custom"
        )

    assert result is not None
    assert result.edge_status == "indirect"
    assert result.is_significant is True
    assert result.refutation_passed is False
    assert result.method_name == "custom"
    assert result.p_value is None
    assert result.stderr is None
    assert result.ci_lower is None


def test_dowhy_tolerates_confidence_interval_failure(
    scalar_frame: pd.DataFrame,
) -> None:
    """Does not fail causal estimation when optional intervals are unavailable."""
    dag = nx.DiGraph([("treatment", "outcome")])
    estimate = MagicMock(value=1.0, p_value=0.2, stderr=0.3)
    estimate.value = 1.0
    estimate.p_value = 0.2
    estimate.stderr = 0.3
    estimate.get_confidence_intervals.side_effect = RuntimeError("unsupported")
    estimate.test_stat_significance.return_value = False
    model = MagicMock()
    model.identify_effect.return_value = "estimand"
    model.estimate_effect.return_value = estimate
    model.refute_estimate.return_value = SimpleNamespace(new_effect=0.9)
    with patch("amarth.estimation.dowhy.CausalModel", return_value=model):
        result = DowhyEstimator(
            confidence_interval_simulations=5
        ).estimate_effect(scalar_frame, dag, "treatment", "outcome")

    assert result is not None
    assert result.is_significant is False
    assert result.ci_lower is None


def test_dowhy_skips_confidence_interval_bootstrap_by_default(
    scalar_frame: pd.DataFrame,
) -> None:
    """Avoids an implicit unbounded bootstrap on the latency-sensitive path."""
    dag = nx.DiGraph([("treatment", "outcome")])
    estimate = MagicMock(value=1.0, p_value=0.2, stderr=0.3)
    estimate.value = 1.0
    estimate.p_value = 0.2
    estimate.stderr = 0.3
    estimate.test_stat_significance.return_value = False
    model = MagicMock()
    model.identify_effect.return_value = "estimand"
    model.estimate_effect.return_value = estimate
    model.refute_estimate.return_value = SimpleNamespace(new_effect=0.9)
    with patch("amarth.estimation.dowhy.CausalModel", return_value=model):
        result = DowhyEstimator().estimate_effect(
            scalar_frame, dag, "treatment", "outcome"
        )

    assert result is not None
    assert result.ci_lower is None
    estimate.get_confidence_intervals.assert_not_called()


def test_dowhy_returns_none_without_path_or_identification(
    scalar_frame: pd.DataFrame,
) -> None:
    """Short-circuits absent causal paths and unidentifiable estimands."""
    disconnected = nx.DiGraph()
    disconnected.add_nodes_from(["treatment", "outcome"])
    assert (
        DowhyEstimator().estimate_effect(
            scalar_frame, disconnected, "treatment", "outcome"
        )
        is None
    )

    connected = nx.DiGraph([("treatment", "outcome")])
    model = MagicMock()
    model.identify_effect.side_effect = ValueError("not identifiable")
    with patch("amarth.estimation.dowhy.CausalModel", return_value=model):
        assert (
            DowhyEstimator().estimate_effect(
                scalar_frame, connected, "treatment", "outcome"
            )
            is None
        )


def test_dowhy_sanitizes_conflicts_and_weakest_cycle_edge() -> None:
    """Produces a DAG by pruning conflicts and the weakest cyclic edge."""
    dag = nx.DiGraph()
    dag.add_edge(
        "conflict",
        "other",
        status="conflict_dir",
        weight=5.0,
    )
    dag.add_edge("a", "b", weight=0.8)
    dag.add_edge("b", "c", weight=0.1)
    dag.add_edge("c", "a", weight=0.6)

    clean = DowhyEstimator()._sanitize_graph(dag)

    assert not clean.has_edge("conflict", "other")
    assert not clean.has_edge("b", "c")
    assert nx.is_directed_acyclic_graph(clean)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"refutation_simulations": -1}, "refutation_simulations"),
        (
            {"confidence_interval_simulations": -1},
            "confidence_interval_simulations",
        ),
        ({"n_jobs": 0}, "n_jobs"),
    ],
)
def test_dowhy_estimator_rejects_invalid_computation_budgets(
    kwargs: dict[str, int], message: str
) -> None:
    """Rejects resource settings that cannot produce bounded execution."""
    with pytest.raises(ValueError, match=message):
        DowhyEstimator(**kwargs)


@pytest.fixture
def embedding_frame() -> pd.DataFrame:
    """Provides embedding-valued confounders and scalar outcomes."""
    return pd.DataFrame(
        {
            "embedding": [
                np.array([0.1, 0.2]),
                np.array([0.2, 0.4]),
                np.array([0.3, 0.6]),
            ],
            "treatment": [0.0, 1.0, 2.0],
            "outcome": [1.0, 2.0, 4.0],
            "prior": [2.0, 2.5, 3.0],
            "mediator": [0.5, 1.0, 1.5],
        }
    )


def test_heterogeneous_estimator_unpacks_embeddings_and_estimates_cates(
    embedding_frame: pd.DataFrame,
) -> None:
    """Fits DML using embedding dimensions and graph-derived confounders."""
    dag = nx.DiGraph(
        [
            ("prior", "treatment"),
            ("treatment", "mediator"),
            ("mediator", "outcome"),
        ]
    )
    inference = MagicMock()
    inference.pvalue.return_value = np.array([0.04])
    econml_wrapper = MagicMock()
    econml_wrapper.effect.return_value = np.array([1.0, 2.0, 3.0])
    econml_wrapper.estimator.ate_inference.return_value = inference
    estimate = SimpleNamespace(estimator=econml_wrapper)
    model = MagicMock()
    model.identify_effect.return_value = "estimand"
    model.estimate_effect.return_value = estimate
    model.refute_estimate.return_value = SimpleNamespace(new_effect=1.9)
    with patch(
        "amarth.estimation.heterogeneous.CausalModel", return_value=model
    ) as model_type:
        result = EmbeddingConfounderEstimator().estimate_effect(
            embedding_frame,
            "treatment",
            "outcome",
            "embedding",
            dag,
        )

    assert result is not None
    assert result.ate == pytest.approx(2.0)
    assert result.cate_std == pytest.approx(np.std([1.0, 2.0, 3.0]))
    assert result.p_value == pytest.approx(0.04)
    assert result.refutation_passed is True
    common_causes = set(model_type.call_args.kwargs["common_causes"])
    assert common_causes == {"embedding_0", "embedding_1", "prior"}
    method_params = model.estimate_effect.call_args.kwargs["method_params"]
    assert method_params["fit_params"] == {"inference": "auto"}
    model.refute_estimate.assert_called_once_with(
        "estimand",
        estimate,
        method_name="data_subset_refuter",
        subset_fraction=0.8,
        num_simulations=5,
        n_jobs=1,
    )


def test_heterogeneous_estimator_handles_unidentifiable_and_missing_p_value(
    embedding_frame: pd.DataFrame,
) -> None:
    """Handles optional DoWhy failures without leaking third-party exceptions."""
    unidentifiable = MagicMock()
    unidentifiable.identify_effect.side_effect = ValueError("unknown")
    with patch(
        "amarth.estimation.heterogeneous.CausalModel",
        return_value=unidentifiable,
    ):
        result = EmbeddingConfounderEstimator().estimate_effect(
            embedding_frame, "treatment", "outcome", "embedding"
        )
    assert result is None

    econml_wrapper = MagicMock()
    econml_wrapper.effect.return_value = np.array([2.0, 2.0, 2.0])
    econml_wrapper.estimator.ate_inference.side_effect = RuntimeError(
        "not available"
    )
    model = MagicMock()
    model.identify_effect.return_value = "estimand"
    model.estimate_effect.return_value = SimpleNamespace(
        estimator=econml_wrapper
    )
    model.refute_estimate.return_value = SimpleNamespace(new_effect=None)
    with patch(
        "amarth.estimation.heterogeneous.CausalModel", return_value=model
    ):
        result = EmbeddingConfounderEstimator(
            discrete_treatment=True
        ).estimate_effect(embedding_frame, "treatment", "outcome", "embedding")
    assert result is not None
    assert result.p_value is None
    assert result.refutation_passed is False


@pytest.mark.parametrize(
    ("samples", "depth", "estimators", "folds", "min_child"),
    [
        (100, 2, 24, 2, 20),
        (1_000, 3, 32, 2, 20),
        (10_000, 4, 64, 3, 200),
        (100_000, 5, 96, 3, 500),
    ],
)
def test_nuisance_models_scale_with_sample_count(
    samples: int,
    depth: int,
    estimators: int,
    folds: int,
    min_child: int,
) -> None:
    """Constrains LightGBM complexity across supported dataset sizes."""
    model_y, model_t, actual_folds = EmbeddingConfounderEstimator(
        discrete_treatment=True
    )._build_nuisance_models(samples)

    assert isinstance(model_y, lgb.LGBMRegressor)
    assert isinstance(model_t, lgb.LGBMClassifier)
    assert model_y.max_depth == depth
    assert model_y.n_estimators == estimators
    assert model_y.min_child_samples == min_child
    assert model_y.n_jobs == 1
    assert actual_folds == folds


def test_continuous_treatment_uses_regression_nuisance_model() -> None:
    """Uses a regressor for a continuous treatment nuisance model."""
    _, model_t, _ = EmbeddingConfounderEstimator()._build_nuisance_models(100)
    assert isinstance(model_t, lgb.LGBMRegressor)


def test_unpack_embeddings_preserves_index_and_removes_vector_column(
    embedding_frame: pd.DataFrame,
) -> None:
    """Expands vectors into stable scalar features without changing row identity."""
    frame = embedding_frame.set_axis([10, 20, 30])
    unpacked, names = EmbeddingConfounderEstimator()._unpack_embeddings(
        frame, "embedding"
    )

    assert names == ["embedding_0", "embedding_1"]
    assert list(unpacked.index) == [10, 20, 30]
    assert "embedding" not in unpacked
    np.testing.assert_allclose(
        unpacked[names].to_numpy(), [[0.1, 0.2], [0.2, 0.4], [0.3, 0.6]]
    )


def test_unpack_embeddings_caps_high_dimensional_vectors() -> None:
    """Bounds DML dimensionality before cross-fitting nuisance models."""
    generator = np.random.default_rng(7)
    matrix = generator.normal(size=(40, 32))
    frame = pd.DataFrame(
        {
            "embedding": list(matrix),
            "treatment": generator.normal(size=40),
            "outcome": generator.normal(size=40),
        }
    )

    unpacked, names = EmbeddingConfounderEstimator(
        max_embedding_components=8
    )._unpack_embeddings(frame, "embedding")

    assert len(names) == 8
    assert unpacked[names].shape == (40, 8)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_embedding_components": 0}, "max_embedding_components"),
        ({"refutation_simulations": -1}, "refutation_simulations"),
        ({"n_jobs": 0}, "n_jobs"),
    ],
)
def test_heterogeneous_estimator_rejects_unbounded_configuration(
    kwargs: dict[str, int], message: str
) -> None:
    """Rejects resource settings that cannot produce bounded execution."""
    with pytest.raises(ValueError, match=message):
        EmbeddingConfounderEstimator(**kwargs)
