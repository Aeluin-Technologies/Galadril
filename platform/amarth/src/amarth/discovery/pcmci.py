"""Tigramite PCMCI adapter for time-lagged causal discovery."""

from __future__ import annotations

import networkx as nx
import numpy as np
import pandas as pd
import structlog
from tigramite import data_processing as pp
from tigramite.independence_tests.parcorr import ParCorr
from tigramite.pcmci import PCMCI

logger = structlog.get_logger(__name__)


class PcmciDiscoverer:
    """Discovers FDR-corrected directional dependencies using Tigramite."""

    def __init__(
        self,
        tau_max: int = 5,
        pc_alpha: float = 0.2,
        mci_alpha: float = 0.05,
        min_effect_size: float = 0.05,
        max_conditioning_dimension: int = 5,
    ) -> None:
        if tau_max < 1:
            raise ValueError("tau_max must be positive")
        if max_conditioning_dimension < 1:
            raise ValueError("max_conditioning_dimension must be positive")
        self.tau_max = tau_max
        self.pc_alpha = pc_alpha
        self.mci_alpha = mci_alpha
        self.min_effect_size = min_effect_size
        self.max_conditioning_dimension = max_conditioning_dimension

    def fit(self, df: pd.DataFrame) -> nx.DiGraph:
        """Runs PCMCI and retains the strongest significant lag per pair."""
        numeric = df.select_dtypes(include=[np.number])
        graph = nx.DiGraph()
        graph.add_nodes_from(numeric.columns)
        if len(numeric) <= (self.tau_max * 3) or len(numeric.columns) < 2:
            return graph

        values = np.ascontiguousarray(
            numeric.to_numpy(dtype=np.float64, copy=False)
        )
        dataset = pp.DataFrame(values, var_names=numeric.columns.tolist())
        pcmci = PCMCI(
            dataframe=dataset,
            cond_ind_test=ParCorr(significance="analytic"),
            verbosity=0,
        )
        result = pcmci.run_pcmci(
            tau_min=1,
            tau_max=self.tau_max,
            pc_alpha=self.pc_alpha,
            alpha_level=self.mci_alpha,
            fdr_method="fdr_bh",
            max_conds_dim=self.max_conditioning_dimension,
        )
        q_matrix = result["p_matrix"]
        value_matrix = result["val_matrix"]
        variables = numeric.columns.tolist()

        for source_index, source in enumerate(variables):
            for target_index, target in enumerate(variables):
                if source_index == target_index:
                    continue
                candidates: list[tuple[float, int, float]] = []
                for lag in range(1, self.tau_max + 1):
                    q_value = float(q_matrix[source_index, target_index, lag])
                    effect = float(
                        value_matrix[source_index, target_index, lag]
                    )
                    if (
                        np.isfinite(q_value)
                        and q_value <= self.mci_alpha
                        and abs(effect) >= self.min_effect_size
                    ):
                        candidates.append((q_value, lag, effect))
                if not candidates:
                    continue

                q_value, lag, effect = min(
                    candidates,
                    key=lambda candidate: (
                        candidate[0],
                        -abs(candidate[2]),
                    ),
                )
                graph.add_edge(
                    source,
                    target,
                    weight=effect,
                    effect_size=effect,
                    optimal_lag=lag,
                    p_value=q_value,
                    q_value=q_value,
                    confidence_score=float(np.clip(1.0 - q_value, 0.0, 1.0)),
                    status="temporal_confirmed",
                    method="tigramite.pcmci",
                )

        logger.info(
            "pcmci_completed",
            edges_found=graph.number_of_edges(),
            variables=len(variables),
            samples=len(numeric),
            tau_max=self.tau_max,
        )
        return graph
