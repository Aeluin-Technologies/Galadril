"""PCMCI causal discovery for time-ordered observational data."""

from enum import Enum

import networkx as nx
import pandas as pd

from amarth.discovery.pcmci import PcmciDiscoverer


class DiscoveryMethod(Enum):
    """Supported causal discovery algorithms."""

    PCMCI = "pcmci"


def discover_graph(
    df: pd.DataFrame, method: DiscoveryMethod = DiscoveryMethod.PCMCI, **kwargs
) -> nx.DiGraph:
    """Discovers a causal DAG from data using the specified method.

    Args:
        df: Input dataframe.
        method: The discovery algorithm. Only PCMCI is supported.
        **kwargs: Additional parameters passed to the underlying discoverer.

    Returns:
        A NetworkX DiGraph representing the causal structure.

    Raises:
        ValueError: If an unsupported method is specified.
    """
    if method != DiscoveryMethod.PCMCI:
        raise ValueError(
            f"PCMCI is the only supported discovery method, received: {method}"
        )

    discoverer = PcmciDiscoverer(
        tau_max=kwargs.get("tau_max", 5),
        pc_alpha=kwargs.get("pc_alpha", 0.05),
        mci_alpha=kwargs.get("mci_alpha", 0.05),
        min_effect_size=kwargs.get("min_effect_size", 0.05),
        max_conditioning_dimension=kwargs.get("max_conditioning_dimension", 5),
    )

    return discoverer.fit(df)
