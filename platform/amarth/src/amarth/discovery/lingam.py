"""DirectLiNGAM algorithm for causal discovery using non-Gaussianity."""

import networkx as nx
import numpy as np
import pandas as pd
from causallearn.search.FCMBased.lingam import DirectLiNGAM


class LingamDiscoverer:
    """Discovers a causal DAG using the DirectLiNGAM algorithm.

    Exploits non-Gaussian continuous data to uniquely identify the causal
    direction. Assumes relations are linear.
    """

    def __init__(self, threshold: float = 0.01):
        """Initializes the DirectLiNGAM discoverer.

        Args:
            threshold: Minimum absolute weight to consider an edge valid.
                       Helps prune numerical noise from the adjacency matrix.
        """
        self.threshold = threshold

    def fit(self, df: pd.DataFrame) -> nx.DiGraph:
        """Fits the DirectLiNGAM algorithm to the dataset.

        Args:
            df: Input data where columns are continuous, non-Gaussian variables.

        Returns:
            A directed acyclic graph (NetworkX) representing strict causal directions.
        """
        X = df.to_numpy()
        node_names = df.columns.tolist()

        model = DirectLiNGAM()
        model.fit(X)

        # The adjacency matrix B in LiNGAM follows the equation: X = B * X + e
        # Therefore, B[i, j] represents the causal effect from X_j to X_i (j -> i).
        adjacency_matrix = model.adjacency_matrix_

        dag = nx.DiGraph()
        dag.add_nodes_from(node_names)

        d = len(node_names)
        for i in range(d):
            for j in range(d):
                weight = adjacency_matrix[i, j]
                if np.abs(weight) > self.threshold:
                    source_node = node_names[j]
                    target_node = node_names[i]
                    dag.add_edge(source_node, target_node, weight=weight)

        return dag
