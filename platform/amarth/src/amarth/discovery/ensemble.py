"""Ensemble causal discovery combining NOTEARS and DirectLiNGAM for cross-validation."""

from enum import Enum
import networkx as nx
import pandas as pd

from amarth.discovery.notears import NotearsDiscoverer
from amarth.discovery.lingam import LingamDiscoverer


class EdgeStatus(str, Enum):
    """Confidence status of a discovered causal edge."""

    CONFIRMED = "confirmed"  # Both NOTEARS and LiNGAM agree on direction.
    CONFLICT_DIRECTION = (
        "conflict_dir"  # Both found a link, but disagree on direction.
    )
    NOTEARS_ONLY = "notears_only"  # Edge only found by NOTEARS.
    LINGAM_ONLY = "lingam_only"  # Edge only found by LiNGAM.


class EnsembleDiscoverer:
    """Discovers a causal DAG using an ensemble of NOTEARS and LiNGAM.

    Cross-validates edges to distinguish high-confidence causal links
    from uncertain correlations requiring LLM or human review.
    """

    def __init__(
        self, notears_threshold: float = 0.3, lingam_threshold: float = 0.01
    ):
        """Initializes the Ensemble discoverer.

        Args:
            notears_threshold: Threshold for NOTEARS adjacency matrix.
            lingam_threshold: Threshold for LiNGAM adjacency matrix.
        """
        self.notears_discoverer = NotearsDiscoverer(threshold=notears_threshold)
        self.lingam_discoverer = LingamDiscoverer(threshold=lingam_threshold)

    def fit(self, df: pd.DataFrame) -> nx.DiGraph:
        """Fits both algorithms and merges their graphs to flag edge confidence.

        Args:
            df: Input data where columns are continuous variables.

        Returns:
            A directed graph (NetworkX) where each edge has a 'status' attribute
            indicating its cross-validation confidence level.
        """
        dag_notears = self.notears_discoverer.fit(df)
        dag_lingam = self.lingam_discoverer.fit(df)

        ensemble_dag = nx.DiGraph()
        ensemble_dag.add_nodes_from(df.columns)

        all_possible_edges = set(dag_notears.edges) | set(dag_lingam.edges)

        for u, v in all_possible_edges:
            in_notears = dag_notears.has_edge(u, v)
            in_lingam = dag_lingam.has_edge(u, v)

            reverse_in_notears = dag_notears.has_edge(v, u)
            reverse_in_lingam = dag_lingam.has_edge(v, u)

            if ensemble_dag.has_edge(v, u):
                continue

            status = None
            edge_to_add = (u, v)

            if in_notears and in_lingam:
                status = EdgeStatus.CONFIRMED

            elif in_notears and reverse_in_lingam:
                status = EdgeStatus.CONFLICT_DIRECTION
                # For conflicts, we add an undirected-like dual representation
                # or arbitrarily keep one with a flag. Here, we keep the LiNGAM
                # direction as it guarantees asymmetric non-Gaussian orientation.
                edge_to_add = (v, u)

            elif in_lingam and reverse_in_notears:
                status = EdgeStatus.CONFLICT_DIRECTION
                edge_to_add = (u, v)

            elif in_notears:
                status = EdgeStatus.NOTEARS_ONLY
            elif in_lingam:
                status = EdgeStatus.LINGAM_ONLY

            if status:
                w_n = (
                    dag_notears[u][v]["weight"]
                    if dag_notears.has_edge(u, v)
                    else 0.0
                )
                w_l = (
                    dag_lingam[u][v]["weight"]
                    if dag_lingam.has_edge(u, v)
                    else 0.0
                )
                weight = (w_n + w_l) / (2.0 if (w_n != 0 and w_l != 0) else 1.0)

                ensemble_dag.add_edge(
                    edge_to_add[0],
                    edge_to_add[1],
                    weight=weight,
                    status=status.value,
                )

        return ensemble_dag
