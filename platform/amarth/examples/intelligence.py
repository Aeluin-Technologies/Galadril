"""Verifiable multi-window intelligence scenario with a regime shift."""

from dataclasses import dataclass
from typing import TypedDict, cast

import networkx as nx
import numpy as np
import pandas as pd
import structlog
from amarth.estimation.dowhy import CausalEstimateResult
from amarth.observations import CausalLink
from amarth.router import AmarthRouter

logger = structlog.get_logger(__name__)

SECONDS_PER_DAY = 86_400.0
EXPECTED_PATROL_ATE = -3.0
PATROL_ATE_TOLERANCE = 0.75


class InferredParameters(TypedDict):
    """Defines the dynamic parameters validated by this example."""

    window_size: str | None
    stability: float


class AnalysisMetadata(TypedDict):
    """Defines router metadata required for verification."""

    is_temporal: bool
    used_embeddings: bool
    counterfactual_ready: bool
    inferred_params: InferredParameters


class AnalysisResults(TypedDict):
    """Defines the typed subset of router output used by this example."""

    consensus_dag: nx.DiGraph
    causal_links: list[CausalLink]
    causal_effects: list[CausalEstimateResult]
    metadata: AnalysisMetadata


@dataclass(frozen=True, slots=True)
class ExpectedRelation:
    """Defines an expected stable relation across intelligence windows."""

    source: str
    target: str
    lag_days: int
    minimum_stability: float = 2.0 / 3.0


EXPECTED_RELATIONS = (
    ExpectedRelation("threat_tension", "intel_alert", lag_days=2),
    ExpectedRelation("intel_alert", "patrol_intensity", lag_days=1),
    ExpectedRelation("threat_tension", "patrol_intensity", lag_days=1),
    ExpectedRelation("patrol_intensity", "incident_rate", lag_days=1),
    ExpectedRelation("threat_tension", "incident_rate", lag_days=2),
    ExpectedRelation("cyber_pressure", "incident_rate", lag_days=3),
)


def _autoregressive_signal(
    generator: np.random.Generator,
    samples: int,
    persistence: float,
) -> np.ndarray:
    """Generates a stationary driver with controlled temporal memory."""
    signal = np.empty(samples, dtype=np.float64)
    signal[0] = generator.normal()
    innovations = generator.normal(size=samples - 1)
    for index in range(1, samples):
        signal[index] = (persistence * signal[index - 1]) + innovations[
            index - 1
        ]
    return signal


def generate_intelligence_scenario(days: int = 600) -> pd.DataFrame:
    """Generates mediated threats, two incident causes, and changing patrol impact."""
    generator = np.random.default_rng(73)
    threat = _autoregressive_signal(generator, days, persistence=0.45)
    cyber = _autoregressive_signal(generator, days, persistence=0.25)

    intel_alert = generator.normal(scale=0.25, size=days)
    intel_alert[2:] += 0.9 * threat[:-2]

    patrol = 10.0 + generator.normal(scale=0.35, size=days)
    patrol[1:] += (0.75 * intel_alert[:-1]) + (0.55 * threat[:-1])

    patrol_effect = np.where(np.arange(days) < (days // 2), -4.0, -2.0)
    incident = 75.0 + generator.normal(scale=0.55, size=days)
    incident[3:] += (
        patrol_effect[2:-1] * patrol[2:-1]
        + (2.4 * threat[1:-2])
        + (1.3 * cyber[:-3])
    )

    embedding_projection = generator.normal(size=(4, 2))
    latent = np.column_stack((threat, cyber))
    embeddings = np.tanh(latent @ embedding_projection.T) + generator.normal(
        scale=0.2, size=(days, 4)
    )
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                start="2026-01-01", periods=days, freq="D", tz="UTC"
            ),
            "threat_tension": threat,
            "intel_alert": intel_alert,
            "patrol_intensity": patrol,
            "cyber_pressure": cyber,
            "incident_rate": incident,
            "intel_embedding": [row.astype(np.float32) for row in embeddings],
        }
    )


def build_prior_graph() -> nx.DiGraph:
    """Builds ontology evidence independently of the generated observations."""
    prior = nx.DiGraph()
    for expected in EXPECTED_RELATIONS:
        prior.add_edge(
            expected.source, expected.target, source="analyst_ontology"
        )
    return prior


def assert_expected_relations(results: AnalysisResults) -> None:
    """Validates stable graph edges and persistable causal-link metadata."""
    dag = results["consensus_dag"]
    links = results["causal_links"]
    assert isinstance(dag, nx.DiGraph), "router returned an invalid graph"
    assert isinstance(links, list), "router returned invalid causal links"

    link_index = {
        (link.source_feature, link.target_feature): link for link in links
    }
    for expected in EXPECTED_RELATIONS:
        assert dag.has_edge(expected.source, expected.target), (
            f"missing stable edge {expected.source} -> {expected.target}; "
            f"discovered={list(dag.edges(data=True))}"
        )
        edge = dag.edges[expected.source, expected.target]
        assert edge["optimal_lag"] == expected.lag_days, (
            f"wrong lag for {expected.source} -> {expected.target}: "
            f"expected={expected.lag_days}, actual={edge['optimal_lag']}"
        )
        assert edge["stability"] >= expected.minimum_stability, (
            f"unstable edge {expected.source} -> {expected.target}: "
            f"stability={edge['stability']:.3f}"
        )
        assert edge["prior_supported"] is True, (
            f"ESKG prior was not attached to {expected.source} -> {expected.target}"
        )

        link = link_index[(expected.source, expected.target)]
        assert link.time_lag_seconds == expected.lag_days * SECONDS_PER_DAY
        assert link.confidence_score >= 0.60
        assert link.supports_counterfactual is True


def run_intelligence_pipeline() -> None:
    """Runs multi-window inference and checks discovery and effect expectations."""
    results = cast(
        AnalysisResults,
        AmarthRouter(
            strict_dag=True,
            max_pcmci_variables=16,
            max_analysis_windows=4,
        ).analyze(
            df=generate_intelligence_scenario(),
            target_outcome="incident_rate",
            time_col="timestamp",
            embedding_col="intel_embedding",
            prior_graph=build_prior_graph(),
            window_size="200D",
        ),
    )

    metadata = results["metadata"]
    assert isinstance(metadata, dict), "router returned invalid metadata"
    inferred = metadata["inferred_params"]
    assert inferred["window_size"] == "200D"
    assert metadata["is_temporal"] is True
    assert metadata["used_embeddings"] is True
    assert metadata["counterfactual_ready"] is True
    assert_expected_relations(results)

    effects = results["causal_effects"]
    assert isinstance(effects, list), "router returned invalid causal effects"
    patrol_effect = next(
        (
            effect
            for effect in effects
            if effect.treatment == "patrol_intensity"
        ),
        None,
    )
    assert patrol_effect is not None, "patrol causal effect was not estimated"
    assert (
        abs(patrol_effect.ate - EXPECTED_PATROL_ATE) <= PATROL_ATE_TOLERANCE
    ), (
        f"patrol ATE outside tolerance: expected={EXPECTED_PATROL_ATE:.3f}, "
        f"actual={patrol_effect.ate:.3f}, "
        f"tolerance={PATROL_ATE_TOLERANCE:.3f}"
    )
    assert patrol_effect.is_significant, "patrol effect is not significant"
    assert patrol_effect.refutation_passed, "patrol refutation failed"

    logger.info(
        "intelligence_validation_passed",
        expected_patrol_ate=EXPECTED_PATROL_ATE,
        actual_patrol_ate=round(patrol_effect.ate, 3),
        checked_relations=len(EXPECTED_RELATIONS),
        discovered_edges=results["consensus_dag"].number_of_edges(),
        window_size=inferred["window_size"],
        stability_threshold=inferred["stability"],
    )


def main() -> int:
    """Returns a process status suitable for local and CI verification."""
    try:
        run_intelligence_pipeline()
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        logger.error("intelligence_validation_failed", error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
