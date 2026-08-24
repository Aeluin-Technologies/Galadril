"""Tests for production Tigramite temporal causal discovery."""

import numpy as np
import pandas as pd
import pytest
from amarth.discovery.pcmci import PcmciDiscoverer


def test_pcmci_reports_direction_confidence_and_lag() -> None:
    """Finds a stable delayed dependency with FDR-corrected confidence."""
    generator = np.random.default_rng(42)
    samples = 600
    cause = generator.normal(size=samples)
    effect = generator.normal(scale=0.05, size=samples)
    effect[3:] += 0.95 * cause[:-3]
    frame = pd.DataFrame(
        {"FacialExpressionShift": cause, "TextSentimentChange": effect}
    )

    graph = PcmciDiscoverer(
        tau_max=6,
        pc_alpha=0.1,
        mci_alpha=0.05,
        min_effect_size=0.1,
    ).fit(frame)

    assert graph.has_edge("FacialExpressionShift", "TextSentimentChange")
    edge = graph.edges["FacialExpressionShift", "TextSentimentChange"]
    assert edge["optimal_lag"] == 3
    assert 0.0 <= edge["confidence_score"] <= 1.0
    assert edge["confidence_score"] > 0.9
    assert edge["method"] == "tigramite.pcmci"
    assert edge["q_value"] <= 0.05


def test_pcmci_rejects_invalid_lag_and_short_or_univariate_data() -> None:
    """Returns empty graphs when temporal inference is statistically impossible."""
    with pytest.raises(ValueError, match="tau_max must be positive"):
        PcmciDiscoverer(tau_max=0)
    with pytest.raises(ValueError, match="max_conditioning_dimension"):
        PcmciDiscoverer(max_conditioning_dimension=0)

    short = pd.DataFrame({"x": [1.0, 2.0], "y": [2.0, 3.0]})
    univariate = pd.DataFrame({"x": np.arange(20, dtype=float)})
    assert PcmciDiscoverer(tau_max=1).fit(short).number_of_edges() == 0
    assert PcmciDiscoverer(tau_max=1).fit(univariate).number_of_edges() == 0
