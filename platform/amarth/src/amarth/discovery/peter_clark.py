"""Peter-Clark algorithm for causal discovery."""

import networkx as nx
import pandas as pd
from causallearn.search.ConstraintBased.PC import pc
from causallearn.utils.cit import fisherz


class PCDiscoverer:
    """Discovers a causal DAG using the Peter-Clark (PC) algorithm.

    Uses conditional independence tests to prune a fully connected graph into
    a Causal Pattern (CPDAG), which is then oriented where possible.
    """

    def __init__(self, alpha: float = 0.05):
        """Initializes the PC discoverer.

        Args:
            alpha: Significance level for conditional independence tests.
        """
        self.alpha = alpha

    def fit(self, df: pd.DataFrame) -> nx.DiGraph:
        """Fits the PC algorithm to the dataset.

        Args:
            df: Input data where columns are variables.

        Returns:
            A directed acyclic graph (NetworkX) representing causal relations.
        """
        X = df.to_numpy()
        node_names = df.columns.tolist()

        cg = pc(
            X, self.alpha, fisherz, node_names=node_names, show_progress=False
        )

        dag = nx.DiGraph()
        dag.add_nodes_from(node_names)

        # Convert causal-learn GeneralGraph to NetworkX DiGraph.
        for edge in cg.G.get_graph_edges():
            node1 = edge.get_node1().get_name()
            node2 = edge.get_node2().get_name()

            ep1 = edge.get_endpoint1()
            ep2 = edge.get_endpoint2()

            # Directed edge: node1 --> node2
            if ep1 == -1 and ep2 == 1:
                dag.add_edge(node1, node2)
            # Directed edge: node2 --> node1
            elif ep1 == 1 and ep2 == -1:
                dag.add_edge(node2, node1)
            # Bi-directed or undirected edges are ignored here to enforce DAG
            # for downstream DoWhy compatibility.

        return dag
