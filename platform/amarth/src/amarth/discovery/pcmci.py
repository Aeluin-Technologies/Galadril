"""Time Series Causal Discovery using a custom PCMCI algorithm."""

import numpy as np
import pandas as pd
import networkx as nx
from scipy import stats
import structlog
from typing import Dict, List, Tuple, Set

logger = structlog.get_logger(__name__)


class PartialCorrelation:
    """Robust Conditional Independence Test using Regularized Residuals & Spearman Rank."""

    @staticmethod
    def test(
        X: np.ndarray, Y: np.ndarray, Z: np.ndarray = None
    ) -> Tuple[float, float]:
        """Computes the partial Spearman correlation between X and Y given Z."""
        if Z is None or Z.shape[1] == 0:
            r, p_val = stats.spearmanr(X, Y)

            if np.isnan(p_val):
                return 0.0, 1.0
            return float(r), float(p_val)
        else:
            Z_with_intercept = np.column_stack([np.ones(len(Z)), Z])
            Z_t_Z = Z_with_intercept.T @ Z_with_intercept
            ridge_penalty = 1e-6 * np.eye(Z_t_Z.shape[0])

            try:
                beta_X = np.linalg.solve(
                    Z_t_Z + ridge_penalty, Z_with_intercept.T @ X
                )
                res_X = X - Z_with_intercept @ beta_X

                beta_Y = np.linalg.solve(
                    Z_t_Z + ridge_penalty, Z_with_intercept.T @ Y
                )
                res_Y = Y - Z_with_intercept @ beta_Y
            except np.linalg.LinAlgError:
                return 0.0, 1.0

            if np.var(res_X) < 1e-10 or np.var(res_Y) < 1e-10:
                return 0.0, 1.0

            r, p_val = stats.spearmanr(res_X, res_Y)
            if np.isnan(p_val):
                return 0.0, 1.0

            return float(r), float(p_val)


class PcmciDiscoverer:
    """Discovers a robust causal Summary DAG from time series data."""

    def __init__(
        self,
        tau_max: int = 5,
        pc_alpha: float = 0.2,
        mci_alpha: float = 0.1,
        min_effect_size: float = 0.05,
    ):
        self.tau_max = tau_max
        self.pc_alpha = pc_alpha
        self.mci_alpha = mci_alpha
        self.min_effect_size = min_effect_size

    def fit(self, df: pd.DataFrame) -> nx.DiGraph:
        """Fits the PCMCI algorithm with FDR correction and returns a DAG."""
        variables = df.columns.tolist()
        data, valid_indices = self._create_lagged_data(df)

        parents = self._run_pc1(data, variables, valid_indices)

        mci_tests = []
        for i, var_x in enumerate(variables):
            for j, var_y in enumerate(variables):
                if i == j:
                    continue

                for tau in range(1, self.tau_max + 1):
                    cond_set = set(parents[var_y]) | self._shift_parents(
                        parents[var_x], tau
                    )

                    source_var = (var_x, tau)
                    if source_var in cond_set:
                        cond_set.remove(source_var)

                    arr_y = data[(var_y, 0)][valid_indices]
                    arr_x = data[source_var][valid_indices]
                    arr_z = (
                        np.column_stack(
                            [data[z][valid_indices] for z in cond_set]
                        )
                        if cond_set
                        else None
                    )

                    val, p_val = PartialCorrelation.test(arr_x, arr_y, arr_z)
                    mci_tests.append(
                        {
                            "source": var_x,
                            "target": var_y,
                            "tau": tau,
                            "val": val,
                            "p_val": p_val,
                        }
                    )

        mci_tests.sort(key=lambda x: x["p_val"])
        m = len(mci_tests)

        summary_dag = nx.DiGraph()
        summary_dag.add_nodes_from(variables)
        best_edges = {}

        for k, test in enumerate(mci_tests):
            fdr_threshold = ((k + 1) / m) * self.mci_alpha

            if (
                test["p_val"] <= fdr_threshold
                and abs(test["val"]) >= self.min_effect_size
            ):
                pair = (test["source"], test["target"])
                if pair not in best_edges or abs(test["val"]) > abs(
                    best_edges[pair]["val"]
                ):
                    best_edges[pair] = test

        for pair, test in best_edges.items():
            summary_dag.add_edge(
                pair[0],
                pair[1],
                weight=test["val"],
                optimal_lag=test["tau"],
                p_value=test["p_val"],
                status="temporal_confirmed",
            )

        logger.info(
            "pcmci_custom_completed",
            edges_found=summary_dag.number_of_edges(),
            total_tests=m,
        )
        return summary_dag

    def _create_lagged_data(
        self, df: pd.DataFrame
    ) -> Tuple[Dict[Tuple[str, int], np.ndarray], np.ndarray]:
        data = {}
        for var in df.columns:
            arr = df[var].to_numpy()
            for tau in range(self.tau_max * 2 + 1):
                if tau == 0:
                    data[(var, tau)] = arr
                else:
                    shifted = np.roll(arr, tau)
                    shifted[:tau] = np.nan
                    data[(var, tau)] = shifted

        valid_indices = np.arange(self.tau_max * 2, len(df))
        return data, valid_indices

    def _run_pc1(
        self, data: Dict, variables: List[str], valid_indices: np.ndarray
    ) -> Dict[str, List[Tuple[str, int]]]:
        parents = {var: [] for var in variables}

        for var_j in variables:
            candidates = [
                (var_i, tau)
                for var_i in variables
                for tau in range(1, self.tau_max + 1)
            ]

            accepted_parents = []

            p_values = {}
            for cand in candidates:
                arr_j = data[(var_j, 0)][valid_indices]
                arr_c = data[cand][valid_indices]
                _, p_val = PartialCorrelation.test(arr_c, arr_j)
                p_values[cand] = p_val

            sorted_cands = [
                c for c in candidates if p_values[c] < self.pc_alpha
            ]
            sorted_cands.sort(key=lambda c: p_values[c])

            for cand in sorted_cands:
                is_independent = False

                arr_j = data[(var_j, 0)][valid_indices]
                arr_c = data[cand][valid_indices]
                arr_z = (
                    np.column_stack(
                        [data[p][valid_indices] for p in accepted_parents]
                    )
                    if accepted_parents
                    else None
                )

                _, p_val = PartialCorrelation.test(arr_c, arr_j, arr_z)
                if p_val >= self.pc_alpha:
                    is_independent = True

                if not is_independent:
                    accepted_parents.append(cand)

            parents[var_j] = accepted_parents

        return parents

    def _shift_parents(
        self, parents: List[Tuple[str, int]], shift_tau: int
    ) -> Set[Tuple[str, int]]:
        shifted = set()
        for var, tau in parents:
            shifted.add((var, tau + shift_tau))
        return shifted
