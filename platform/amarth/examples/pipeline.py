"""Verifiable temporal discovery and heterogeneous-effect example."""

from dataclasses import dataclass

import networkx as nx
import numpy as np
import pandas as pd
import structlog
from amarth.discovery import DiscoveryMethod, discover_graph
from amarth.estimation.dowhy import DowhyEstimator
from amarth.estimation.heterogeneous import EmbeddingConfounderEstimator

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExpectedRelation:
    """Defines one deterministic causal-discovery acceptance criterion."""

    source: str
    target: str
    lag: int
    minimum_confidence: float = 0.95


EXPECTED_RELATIONS = (
    ExpectedRelation("feature_X", "mediator_M", lag=2),
    ExpectedRelation("mediator_M", "outcome_Y", lag=1),
    ExpectedRelation("feature_X", "outcome_Y", lag=3),
    ExpectedRelation("secondary_S", "outcome_Y", lag=4),
)
EXPECTED_TOTAL_EFFECT = 1.2 + (0.85 * 0.65)
EFFECT_TOLERANCE = 0.35


def generate_synthetic_data(
    n_samples: int = 1_000,
) -> pd.DataFrame:
    """Generates direct, mediated, heterogeneous, and secondary lagged effects."""
    generator = np.random.default_rng(42)
    feature = generator.normal(size=n_samples)
    secondary = generator.normal(size=n_samples)
    context = generator.normal(size=n_samples)
    mediator = generator.normal(scale=0.25, size=n_samples)
    outcome = generator.normal(scale=0.35, size=n_samples)

    mediator[2:] += 0.85 * feature[:-2]
    heterogeneous_effect = 1.2 + (0.3 * np.tanh(context))
    outcome[4:] += (
        heterogeneous_effect[1:-3] * feature[1:-3]
        + (0.65 * mediator[3:-1])
        - (0.55 * secondary[:-4])
    )

    projection = generator.normal(size=(32, 2))
    latent = np.column_stack((context, np.square(context)))
    embeddings = np.tanh(latent @ projection.T) + generator.normal(
        scale=0.35, size=(n_samples, 32)
    )
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-08-24", periods=n_samples, freq="1s", tz="UTC"
            ),
            "feature_X": feature,
            "mediator_M": mediator,
            "secondary_S": secondary,
            "outcome_Y": outcome,
            "embedding": [row.astype(np.float32) for row in embeddings],
        }
    )


def assert_expected_relations(dag: nx.DiGraph) -> None:
    """Validates direction, lag, confidence, and corrected significance."""
    for expected in EXPECTED_RELATIONS:
        assert dag.has_edge(expected.source, expected.target), (
            f"missing expected edge {expected.source} -> {expected.target}; "
            f"discovered={list(dag.edges(data=True))}"
        )
        edge = dag.edges[expected.source, expected.target]
        assert edge["optimal_lag"] == expected.lag, (
            f"wrong lag for {expected.source} -> {expected.target}: "
            f"expected={expected.lag}, actual={edge['optimal_lag']}"
        )
        assert edge["confidence_score"] >= expected.minimum_confidence, (
            f"weak confidence for {expected.source} -> {expected.target}: "
            f"{edge['confidence_score']:.3f}"
        )
        assert edge["q_value"] <= 0.05, (
            f"edge {expected.source} -> {expected.target} failed FDR: "
            f"q={edge['q_value']:.4g}"
        )


def run_pipeline() -> None:
    """Runs the example and raises precise assertions for incorrect computation."""
    frame = generate_synthetic_data()
    discovery_frame = frame[
        ["feature_X", "mediator_M", "secondary_S", "outcome_Y"]
    ]
    dag = discover_graph(
        discovery_frame,
        method=DiscoveryMethod.PCMCI,
        tau_max=6,
        pc_alpha=0.1,
        mci_alpha=0.05,
        min_effect_size=0.1,
        max_conditioning_dimension=5,
    )
    logger.info("discovered_edges", edges=list(dag.edges(data=True)))
    assert_expected_relations(dag)

    direct_edge = dag.edges["feature_X", "outcome_Y"]
    lag = int(direct_edge["optimal_lag"])
    aligned = pd.DataFrame(
        {
            "feature_X": frame["feature_X"].iloc[:-lag].to_numpy(copy=False),
            "outcome_Y": frame["outcome_Y"].iloc[lag:].to_numpy(copy=False),
            "embedding": frame["embedding"].iloc[:-lag].to_list(),
        }
    )
    effect_dag = nx.DiGraph()
    effect_dag.add_edge("feature_X", "outcome_Y", **direct_edge)

    classic = DowhyEstimator(strict_dag=True).estimate_effect(
        df=aligned[["feature_X", "outcome_Y"]],
        dag=effect_dag,
        treatment="feature_X",
        outcome="outcome_Y",
    )
    assert classic is not None, "classic DoWhy estimation returned no result"
    assert abs(classic.ate - EXPECTED_TOTAL_EFFECT) <= EFFECT_TOLERANCE, (
        f"classic ATE outside tolerance: expected={EXPECTED_TOTAL_EFFECT:.3f}, "
        f"actual={classic.ate:.3f}, tolerance={EFFECT_TOLERANCE:.3f}"
    )

    heterogeneous = EmbeddingConfounderEstimator(
        max_embedding_components=16,
        refutation_simulations=5,
        n_jobs=1,
    ).estimate_effect(
        df=aligned,
        treatment="feature_X",
        outcome="outcome_Y",
        embedding_col="embedding",
        dag=effect_dag,
    )
    assert heterogeneous is not None, "EconML estimation returned no result"
    assert abs(heterogeneous.ate - EXPECTED_TOTAL_EFFECT) <= EFFECT_TOLERANCE, (
        f"heterogeneous ATE outside tolerance: "
        f"expected={EXPECTED_TOTAL_EFFECT:.3f}, actual={heterogeneous.ate:.3f}"
    )
    assert heterogeneous.p_value is not None, "EconML p-value is unavailable"
    assert heterogeneous.p_value <= 0.05, (
        f"heterogeneous effect is not significant: p={heterogeneous.p_value:.4g}"
    )
    assert heterogeneous.cate_std >= 0.05, (
        "heterogeneous effect collapsed to an effectively constant estimate"
    )
    assert heterogeneous.refutation_passed, "DoWhy subset refutation failed"

    logger.info(
        "pipeline_validation_passed",
        expected_total_effect=round(EXPECTED_TOTAL_EFFECT, 3),
        classic_ate=round(classic.ate, 3),
        heterogeneous_ate=round(heterogeneous.ate, 3),
        cate_std=round(heterogeneous.cate_std, 3),
        p_value=heterogeneous.p_value,
        checked_relations=len(EXPECTED_RELATIONS),
    )


def main() -> int:
    """Returns a process status suitable for local and CI verification."""
    try:
        run_pipeline()
    except (AssertionError, TypeError, ValueError) as exc:
        logger.error("pipeline_validation_failed", error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
