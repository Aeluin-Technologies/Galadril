"""Time Series causal discovery using Tigramite (PCMCI algorithm)."""

import networkx as nx
import numpy as np
import pandas as pd
import structlog

from tigramite import data_processing as pp
from tigramite.pcmci import PCMCI
from tigramite.independence_tests.parcorr import ParCorr
from tigramite.independence_tests.cmiknn import CMIknn

logger = structlog.get_logger(__name__)


class TigramiteDiscoverer:
    """Discovers a causal DAG from time series data using the PCMCI algorithm.

    Supports both fast linear relationships and rigorous non-linear real-world
    dynamics (health, defense) using Information Theory.
    """

    def __init__(
        self,
        tau_max: int = 5,
        pc_alpha: float = 0.05,
        assume_linear: bool = False,
    ):
        """Initializes the Tigramite PCMCI discoverer.

        Args:
            tau_max: The maximum time lag to investigate (in data steps).
            pc_alpha: The significance level for the conditional independence tests.
            assume_linear: If True, uses fast Partial Correlation (ParCorr).
                           If False (default), uses Conditional Mutual Information (CMIknn)
                           to capture real-world non-linear effects.
        """
        self.tau_max = tau_max
        self.pc_alpha = pc_alpha
        self.assume_linear = assume_linear

    def fit(self, df: pd.DataFrame) -> nx.DiGraph:
        """Fits the PCMCI algorithm to the multivariate time series."""
        node_names = df.columns.tolist()
        n_vars = len(node_names)

        dataframe = pp.DataFrame(
            df.to_numpy(), datatime=np.arange(len(df)), var_names=node_names
        )

        # Akatosh would say everything is linear and ordered.
        if self.assume_linear:
            logger.info("using_linear_test_parcorr")
            cond_ind_test = ParCorr(significance="analytic")
        else:
            logger.info("using_non_linear_test_cmiknn")
            cond_ind_test = CMIknn(
                significance="shuffle_test", knn=int(max(10, 0.1 * len(df)))
            )

        pcmci = PCMCI(
            dataframe=dataframe, cond_ind_test=cond_ind_test, verbosity=0
        )

        logger.info(
            "running_pcmci", vars=n_vars, samples=len(df), tau_max=self.tau_max
        )

        results = pcmci.run_pcmci(tau_max=self.tau_max, pc_alpha=self.pc_alpha)

        p_matrix = results["p_matrix"]
        val_matrix = results["val_matrix"]

        dag = nx.DiGraph()
        dag.add_nodes_from(node_names)

        for i in range(n_vars):
            for j in range(n_vars):
                if i == j:
                    continue

                best_lag = None
                best_val = 0.0
                min_p = float("inf")

                for tau in range(self.tau_max + 1):
                    p_val = p_matrix[i, j, tau]
                    val = val_matrix[i, j, tau]

                    if p_val < self.pc_alpha:
                        if abs(val) > abs(best_val):
                            best_val = val
                            best_lag = tau
                            min_p = p_val

                if best_lag is not None:
                    dag.add_edge(
                        node_names[i],
                        node_names[j],
                        weight=float(best_val),
                        optimal_lag=int(best_lag),
                        p_value=float(min_p),
                        status="temporal_confirmed",
                    )

        return dag
