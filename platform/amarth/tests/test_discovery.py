"""Unit tests for the PCMCI-only causal discovery facade."""

from unittest.mock import patch

import networkx as nx
import pandas as pd
import pytest
from amarth.discovery import DiscoveryMethod, discover_graph


@pytest.fixture
def frame() -> pd.DataFrame:
    """Provides deterministic time-ordered observations."""
    return pd.DataFrame({"x": [0.0, 1.0, 2.0], "y": [1.0, 2.0, 4.0]})


def test_discover_graph_delegates_to_pcmci(frame: pd.DataFrame) -> None:
    """Configures the sole production discovery algorithm explicitly."""
    expected = nx.DiGraph()
    with patch("amarth.discovery.PcmciDiscoverer") as discoverer_type:
        discoverer_type.return_value.fit.return_value = expected

        actual = discover_graph(
            frame,
            method=DiscoveryMethod.PCMCI,
            tau_max=3,
            pc_alpha=0.1,
            mci_alpha=0.2,
            min_effect_size=0.3,
        )

    assert actual is expected
    discoverer_type.assert_called_once_with(
        tau_max=3,
        pc_alpha=0.1,
        mci_alpha=0.2,
        min_effect_size=0.3,
        max_conditioning_dimension=5,
    )
    discoverer_type.return_value.fit.assert_called_once_with(frame)


def test_discover_graph_rejects_unknown_method(frame: pd.DataFrame) -> None:
    """Rejects legacy static algorithms instead of silently invoking them."""
    with pytest.raises(ValueError, match="PCMCI is the only supported"):
        discover_graph(frame, method="notears")  # type: ignore[arg-type]
