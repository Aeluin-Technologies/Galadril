"""Causal discovery module for generating DAGs from observational data."""

from enum import Enum
import networkx as nx
import pandas as pd

from amarth.discovery.notears import NotearsDiscoverer
from amarth.discovery.peter_clark import PCDiscoverer
from amarth.discovery.lingam import LingamDiscoverer
from amarth.discovery.ensemble import EnsembleDiscoverer
from amarth.discovery.pcmci import PcmciDiscoverer


class DiscoveryMethod(Enum):
    """Supported causal discovery algorithms."""

    NOTEARS = "notears"
    PC = "pc"
    LINGAM = "lingam"
    ENSEMBLE = "ensemble"
    PCMCI = "pcmci"


def discover_graph(
    df: pd.DataFrame, method: DiscoveryMethod = DiscoveryMethod.LINGAM, **kwargs
) -> nx.DiGraph:
    """Discovers a causal DAG from data using the specified method.

    Args:
        df: Input dataframe.
        method: The discovery algorithm to use. Defaults to LINGAM for robust directionality.
        **kwargs: Additional parameters passed to the underlying discoverer.

    Returns:
        A NetworkX DiGraph representing the causal structure.

    Raises:
        ValueError: If an unsupported method is specified.
    """
    if method == DiscoveryMethod.NOTEARS:
        discoverer = NotearsDiscoverer(
            threshold=kwargs.get("threshold", 0.3),
            max_iter=kwargs.get("max_iter", 100),
        )
    elif method == DiscoveryMethod.PC:
        discoverer = PCDiscoverer(alpha=kwargs.get("alpha", 0.05))
    elif method == DiscoveryMethod.LINGAM:
        discoverer = LingamDiscoverer(threshold=kwargs.get("threshold", 0.01))
    elif method == DiscoveryMethod.ENSEMBLE:
        discoverer = EnsembleDiscoverer(
            notears_threshold=kwargs.get("notears_threshold", 0.3),
            lingam_threshold=kwargs.get("lingam_threshold", 0.01),
        )
    elif method == DiscoveryMethod.PCMCI:
        discoverer = PcmciDiscoverer(
            tau_max=kwargs.get("tau_max", 5),
            pc_alpha=kwargs.get("pc_alpha", 0.05),
            mci_alpha=kwargs.get("mci_alpha", 0.05),
        )
    else:
        raise ValueError(f"Unsupported discovery method: {method}")

    return discoverer.fit(df)
