"""Tests for persistable causal-link enrichment."""

import networkx as nx
from amarth.router import AmarthRouter


def test_causal_link_contains_lag_confidence_and_eskg_provenance() -> None:
    """Converts a discovered lag into the required first-class graph contract."""
    graph = nx.DiGraph()
    graph.add_edge(
        "FacialExpressionShift.confidence",
        "TextSentimentChange.sentiment",
        confidence_score=0.87,
        effect_size=0.62,
        optimal_lag=3,
        p_value=0.01,
        q_value=0.02,
        stability=1.0,
        method="tigramite.pcmci",
    )

    links = AmarthRouter()._build_causal_links(
        graph,
        sampling_interval_seconds=1.0,
        feature_node_ids={
            "FacialExpressionShift": ("vision-event",),
            "TextSentimentChange": ("text-state",),
        },
    )

    assert len(links) == 1
    link = links[0]
    assert link.confidence_score == 0.87
    assert link.time_lag_seconds == 3.0
    assert link.source_node_ids == ("vision-event",)
    assert link.target_node_ids == ("text-state",)
    assert link.supports_counterfactual is True
