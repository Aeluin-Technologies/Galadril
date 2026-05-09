"""Router module for automated causal pipeline selection and execution."""

from typing import Any, Dict, List, Optional
import networkx as nx
import numpy as np
import pandas as pd
import structlog

from amarth.discovery import discover_graph, DiscoveryMethod
from amarth.estimation.dowhy import DowhyEstimator
from amarth.estimation.heterogeneous import EmbeddingConfounderEstimator

logger = structlog.get_logger(__name__)


class AmarthRouter:
    """Routes data through the optimal causal discovery and estimation path."""

    def __init__(
        self,
        min_window_samples: int = 100,
        stability_threshold: float = 0.7,
        strict_dag: bool = True
    ):
        """Initializes the Amarth Router.

        Args:
            min_window_samples: Minimum samples required per time window.
            stability_threshold: Fraction of windows an edge must appear in to be considered stable.
            strict_dag: If True, forces the final consensus graph to be acyclic.
        """
        self.min_window_samples = min_window_samples
        self.stability_threshold = stability_threshold
        self.strict_dag = strict_dag

    def analyze(
        self,
        df: pd.DataFrame,
        target_outcome: str,
        time_col: Optional[str] = None,
        embedding_col: Optional[str] = None,
        prior_graph: Optional[nx.DiGraph] = None,
        window_size: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Executes the full automated causal pipeline.

        Args:
            df: Raw input dataframe (can contain NaNs and noise).
            target_outcome: The variable we want to find causes for.
            time_col: Column name containing timestamps (triggers Temporal Pipeline).
            embedding_col: Column name containing vector embeddings (triggers Heterogeneous DML).
            prior_graph: Gold layer correlations. Used to restrict the search space.
            window_size: Pandas frequency string (e.g., '30D' for 30 days).

        Returns:
            Dictionary containing the consensus DAG and the estimated causal effects.
        """
        logger.info("amarth_router_started", rows=len(df), cols=len(df.columns))

        df_clean = self._preprocess_data(df, is_temporal=bool(time_col))

        if time_col and window_size:
            consensus_dag = self._windowed_temporal_discovery(
                df_clean, time_col, window_size, prior_graph
            )
        else:
            consensus_dag = self._static_discovery(df_clean, prior_graph)

        effects = self._estimate_effects(
            df_clean, consensus_dag, target_outcome, embedding_col
        )

        return {
            "consensus_dag": consensus_dag,
            "causal_effects": effects,
            "metadata": {
                "samples_processed": len(df_clean),
                "is_temporal": bool(time_col),
                "used_embeddings": bool(embedding_col)
            }
        }

    def _preprocess_data(self, df: pd.DataFrame, is_temporal: bool) -> pd.DataFrame:
        """Cleans noisy data and imputes missing values."""
        df_clean = df.copy()
        
        num_cols = df_clean.select_dtypes(include=[np.number]).columns
        if is_temporal:
            # Time-aware interpolation for missing sensor/state data.
            df_clean[num_cols] = df_clean[num_cols].interpolate(method='linear').bfill().ffill()
        else:
            # Median imputation for static data to ignore outliers.
            df_clean[num_cols] = df_clean[num_cols].fillna(df_clean[num_cols].median())

        # Forward-fill embeddings if missing (assuming context persists).
        emb_cols = [c for c in df_clean.columns if df_clean[c].apply(lambda x: isinstance(x, (list, np.ndarray))).any()]
        for c in emb_cols:
            df_clean[c] = df_clean[c].ffill().bfill()

        return df_clean

    def _windowed_temporal_discovery(
        self, 
        df: pd.DataFrame, 
        time_col: str, 
        window_size: str,
        prior_graph: Optional[nx.DiGraph]
    ) -> nx.DiGraph:
        """Runs discovery over rolling windows to handle non-stationarity.
        
        Complexity: O(W * TIGRAMITE) where W is number of windows.
        """
        logger.info("running_windowed_temporal_discovery", window=window_size)
        df_ts = df.set_index(time_col).sort_index()
        
        # We assume LPCMCI (Latent PCMCI) via Tigramite to handle hidden variables
        # Fallback to standard Tigramite if LPCMCI is not explicitly registered yet.
        method = DiscoveryMethod.TIGRAMITE 
        
        window_dags = []
        for _, window_df in df_ts.resample(window_size):
            if len(window_df) < self.min_window_samples:
                continue
                
            # Exclude non-scalar columns for the graph discovery phase.
            scalar_df = window_df.select_dtypes(include=[np.number])
            
            try:
                # TODO: Inject prior_graph as background_knowledge to Tigramite.
                dag = discover_graph(scalar_df, method=method, assume_linear=False)
                window_dags.append(dag)
            except Exception as e:
                logger.warning("window_discovery_failed", error=str(e))

        return self._aggregate_dags(window_dags)

    def _static_discovery(
        self, 
        df: pd.DataFrame, 
        prior_graph: Optional[nx.DiGraph]
    ) -> nx.DiGraph:
        """Runs discovery on static data using Latent variable algorithms (FCI/Ensemble)."""
        scalar_df = df.select_dtypes(include=[np.number])
        return discover_graph(scalar_df, method=DiscoveryMethod.ENSEMBLE)

    def _aggregate_dags(self, dags: List[nx.DiGraph]) -> nx.DiGraph:
        """Aggregates multiple windowed DAGs into a stable consensus DAG.
        
        Only keeps edges that appear in >= stability_threshold % of windows.
        This isolates fundamental causal rules from transient regime shifts.
        """
        if not dags:
            return nx.DiGraph()

        edge_counts = {}
        total_windows = len(dags)

        for dag in dags:
            for u, v in dag.edges():
                edge_counts[(u, v)] = edge_counts.get((u, v), 0) + 1

        consensus = nx.DiGraph()
        if dags:
            consensus.add_nodes_from(dags[0].nodes())

        for (u, v), count in edge_counts.items():
            stability = count / total_windows
            if stability >= self.stability_threshold:
                consensus.add_edge(u, v, stability=stability, status="stable_temporal")

        if self.strict_dag and not nx.is_directed_acyclic_graph(consensus):
            consensus = self._break_cycles(consensus)

        return consensus

    def _estimate_effects(
        self, 
        df: pd.DataFrame, 
        dag: nx.DiGraph, 
        target_outcome: str, 
        embedding_col: Optional[str]
    ) -> List[Any]:
        """Routes to the optimal estimator based on data structure.
        
        Complexity: O(E * Estimation)
        """
        results = []
        predecessors = list(dag.predecessors(target_outcome))
        
        if not predecessors:
            logger.info("no_causal_ancestors_found", target=target_outcome)
            return results

        if embedding_col and embedding_col in df.columns:
            logger.info("routing_to_heterogeneous_estimator", confounder=embedding_col)
            estimator = EmbeddingConfounderEstimator(discrete_treatment=False)
            
            for treatment in predecessors:
                res = estimator.estimate_effect(
                    df=df,
                    treatment=treatment,
                    outcome=target_outcome,
                    embedding_col=embedding_col,
                    dag=dag
                )
                if res and res.refutation_passed:
                    results.append(res)
        else:
            estimator = DowhyEstimator(strict_dag=True)
            
            for treatment in predecessors:
                res = estimator.estimate_effect(
                    df=df,
                    dag=dag,
                    treatment=treatment,
                    outcome=target_outcome
                )
                if res and res.refutation_passed:
                    results.append(res)

        return results

    def _break_cycles(self, dag: nx.DiGraph) -> nx.DiGraph:
        """Removes the least stable edges to break cycles."""
        clean_dag = dag.copy()
        while not nx.is_directed_acyclic_graph(clean_dag):
            try:
                cycle = nx.find_cycle(clean_dag)
                weakest = min(cycle, key=lambda e: clean_dag[e[0]][e[1]].get('stability', 1.0))
                clean_dag.remove_edge(*weakest)
            except nx.NetworkXNoCycle:
                break
        return clean_dag
