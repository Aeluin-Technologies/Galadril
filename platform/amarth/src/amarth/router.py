"""Router module for automated causal pipeline selection and execution.

This module orchestrates the end-to-end causal inference pipeline. It handles
messy data (NaNs, noise), dynamic parameter inference, non-stationarity
(via time windows), high-dimensional confounders (via PCA and Double ML),
and temporal alignment for accurate causal effect estimation.
"""

from typing import Any, Dict, List, Optional
import networkx as nx
import numpy as np
import pandas as pd
import structlog
from sklearn.decomposition import PCA

from amarth.discovery import discover_graph, DiscoveryMethod
from amarth.estimation.dowhy import DowhyEstimator
from amarth.estimation.heterogeneous import EmbeddingConfounderEstimator

logger = structlog.get_logger(__name__)


class AmarthRouter:
    """Routes data through the optimal causal discovery and estimation path."""

    def __init__(self, strict_dag: bool = True):
        """Initializes the Amarth Router.

        Args:
            strict_dag (bool): If True, forces the final consensus causal graph
                to be a strictly Directed Acyclic Graph (DAG) by breaking cycles
                based on edge stability. Defaults to True.
        """
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
            df (pd.DataFrame): Raw input dataframe containing the variables.
                Can contain missing values and noise.
            target_outcome (str): The name of the target variable we want to
                find causes and effects for.
            time_col (Optional[str]): Column name containing timestamps. If provided,
                triggers the temporal discovery pipeline. Defaults to None.
            embedding_col (Optional[str]): Column name containing vector embeddings.
                If provided, triggers the Heterogeneous DML estimator. Defaults to None.
            prior_graph (Optional[nx.DiGraph]): A prior knowledge graph (e.g., from
                a Gold layer) to restrict or guide the search space. Defaults to None.
            window_size (Optional[str]): Pandas frequency string (e.g., '30D', '1H').
                If provided, overrides the dynamically inferred window size.
                Defaults to None.

        Returns:
            Dict[str, Any]: A dictionary containing:
                - "consensus_dag" (nx.DiGraph): The final discovered causal graph.
                - "causal_effects" (List[Any]): List of causal effect estimation results.
                - "metadata" (Dict[str, Any]): Processing metadata and inferred parameters.
        """
        logger.info("amarth_router_started", rows=len(df), cols=len(df.columns))

        df_clean = self._preprocess_data(df, is_temporal=bool(time_col))

        params = self._infer_dynamic_parameters(
            df_clean, time_col, user_window_size=window_size
        )
        logger.info("inferred_parameters", **params)

        if embedding_col and embedding_col in df_clean.columns:
            df_clean = self._reduce_embeddings(df_clean, embedding_col)

        if time_col:
            consensus_dag = self._windowed_temporal_discovery(
                df=df_clean,
                time_col=time_col,
                window_size=params["window_size"],
                tau_max=params["tau_max"],
                stability_threshold=params["stability"],
                prior_graph=prior_graph,
            )
            # Align the dataframe so X_t-lag is on the same row as Y_t.
            df_aligned = self._align_temporal_data(
                df_clean, consensus_dag, target_outcome
            )
        else:
            consensus_dag = self._static_discovery(df_clean, prior_graph)
            df_aligned = df_clean

        effects = self._estimate_effects(
            df=df_aligned,
            dag=consensus_dag,
            target_outcome=target_outcome,
            embedding_col=embedding_col,
        )

        return {
            "consensus_dag": consensus_dag,
            "causal_effects": effects,
            "metadata": {
                "samples_processed": len(df_aligned),
                "is_temporal": bool(time_col),
                "used_embeddings": bool(embedding_col),
                "inferred_params": params,
            },
        }

    def _infer_dynamic_parameters(
        self,
        df: pd.DataFrame,
        time_col: Optional[str],
        user_window_size: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Calculates optimal causal parameters based on dataset shape.

        Respects the user override for window_size while adjusting stability
        and lag parameters accordingly.

        Args:
            df (pd.DataFrame): The input dataframe.
            time_col (Optional[str]): The column name representing time.
            user_window_size (Optional[str]): A forced window size from the user.

        Returns:
            Dict[str, Any]: A dictionary containing 'window_size', 'tau_max',
                'stability', and 'min_window_samples'.
        """
        n_samples = len(df)

        if not time_col:
            return {
                "window_size": None,
                "tau_max": 0,
                "stability": 0.5,
                "min_window_samples": n_samples // 3,
            }

        df_time = pd.to_datetime(df[time_col])
        duration = df_time.max() - df_time.min()

        if duration <= pd.Timedelta(days=1):
            default_window = "1H"
            tau_max = min(10, n_samples // 20)
        elif duration <= pd.Timedelta(days=30):
            default_window = "1D"
            tau_max = min(5, n_samples // 10)
        elif duration <= pd.Timedelta(days=365):
            default_window = "30D"
            tau_max = min(7, n_samples // 10)
        else:
            default_window = "365D"
            tau_max = min(5, n_samples // 20)

        window = user_window_size if user_window_size else default_window

        if n_samples < 100 and not user_window_size:
            window = f"{duration.days + 1}D"
            stability = 1.0
            min_samples = max(20, int(n_samples * 0.8))
        else:
            try:
                estimated_windows = max(
                    1, int(duration / pd.to_timedelta(window))
                )
            except Exception:
                estimated_windows = 1

            stability = max(0.15, 1.0 / estimated_windows)
            min_samples = 30

        return {
            "window_size": window,
            "tau_max": max(1, tau_max),
            "stability": float(stability),
            "min_window_samples": min_samples,
        }

    def _preprocess_data(
        self, df: pd.DataFrame, is_temporal: bool
    ) -> pd.DataFrame:
        """Cleans noisy data and imputes missing values.

        Args:
            df (pd.DataFrame): The raw dataframe.
            is_temporal (bool): Flag indicating if the data is a time series.

        Returns:
            pd.DataFrame: The cleaned dataframe with imputed values.
        """
        df_clean = df.copy()
        df_clean = df_clean.dropna(axis=1, how="all")

        num_cols = df_clean.select_dtypes(include=[np.number]).columns
        if is_temporal:
            df_clean[num_cols] = (
                df_clean[num_cols].interpolate(method="linear").bfill().ffill()
            )
        else:
            df_clean[num_cols] = df_clean[num_cols].fillna(
                df_clean[num_cols].median()
            )

        emb_cols = [
            c
            for c in df_clean.columns
            if df_clean[c]
            .apply(lambda x: isinstance(x, (list, np.ndarray)))
            .any()
        ]
        for c in emb_cols:
            df_clean[c] = df_clean[c].ffill().bfill()

        return df_clean

    def _reduce_embeddings(
        self, df: pd.DataFrame, embedding_col: str
    ) -> pd.DataFrame:
        """Applies PCA to embeddings to retain variance and prevent DML overfitting.

        Args:
            df (pd.DataFrame): Dataframe containing the embedding column.
            embedding_col (str): The name of the embedding column.

        Returns:
            pd.DataFrame: Dataframe with reduced dimensionality embeddings.
        """
        matrix = np.stack(df[embedding_col].values)

        max_components = min(matrix.shape[1], max(2, len(df) // 10))

        if matrix.shape[1] > max_components:
            logger.info(
                "applying_pca_to_embeddings",
                orig_dim=matrix.shape[1],
                target_dim=max_components,
            )
            pca = PCA(n_components=0.95, svd_solver="full")
            reduced = pca.fit_transform(matrix)

            if reduced.shape[1] > max_components:
                reduced = reduced[:, :max_components]

            df[embedding_col] = list(reduced)

        return df

    def _windowed_temporal_discovery(
        self,
        df: pd.DataFrame,
        time_col: str,
        window_size: str,
        tau_max: int,
        stability_threshold: float,
        prior_graph: Optional[nx.DiGraph],
    ) -> nx.DiGraph:
        """Runs causal discovery over rolling windows to handle non-stationarity.

        Args:
            df (pd.DataFrame): The time series dataframe.
            time_col (str): Column name containing timestamps.
            window_size (str): Pandas frequency string for the sliding window.
            tau_max (int): Maximum lag to test.
            stability_threshold (float): Required appearance fraction to keep an edge.
            prior_graph (Optional[nx.DiGraph]): Prior knowledge to restrict search.

        Returns:
            nx.DiGraph: The consensus Summary DAG.
        """
        logger.info("running_windowed_temporal_discovery")
        df_ts = df.set_index(time_col).sort_index()
        method = DiscoveryMethod.PCMCI
        window_dags = []

        for _, window_df in df_ts.resample(window_size):
            if len(window_df) < 30:
                continue

            scalar_df = window_df.select_dtypes(include=[np.number])
            try:
                dag = discover_graph(
                    scalar_df,
                    method=method,
                    tau_max=tau_max,
                    assume_linear=False,
                )
                window_dags.append(dag)
            except Exception as e:
                logger.debug("window_discovery_failed", error=str(e))

        return self._aggregate_dags(window_dags, stability_threshold)

    def _static_discovery(
        self, df: pd.DataFrame, prior_graph: Optional[nx.DiGraph]
    ) -> nx.DiGraph:
        """Runs discovery on static data using Latent variable algorithms.

        Args:
            df (pd.DataFrame): Dataframe.
            prior_graph (Optional[nx.DiGraph]): Prior graph to guide search.

        Returns:
            nx.DiGraph: The discovered DAG.
        """
        scalar_df = df.select_dtypes(include=[np.number])
        return discover_graph(scalar_df, method=DiscoveryMethod.ENSEMBLE)

    def _aggregate_dags(
        self, dags: List[nx.DiGraph], stability_threshold: float
    ) -> nx.DiGraph:
        """Aggregates multiple DAGs into a stable consensus DAG.

        Calculates the stability of each edge and computes the median optimal lag.

        Args:
            dags (List[nx.DiGraph]): List of graphs discovered across windows.
            stability_threshold (float): Minimum required stability (0.0 to 1.0).

        Returns:
            nx.DiGraph: The stable consensus graph.
        """
        if not dags:
            return nx.DiGraph()

        edge_stats = {}
        total_windows = len(dags)

        for dag in dags:
            for u, v, data in dag.edges(data=True):
                if (u, v) not in edge_stats:
                    edge_stats[(u, v)] = {"count": 0, "lags": []}
                edge_stats[(u, v)]["count"] += 1
                if "optimal_lag" in data:
                    edge_stats[(u, v)]["lags"].append(data["optimal_lag"])

        consensus = nx.DiGraph()
        if dags:
            consensus.add_nodes_from(dags[0].nodes())

        for (u, v), stats in edge_stats.items():
            stability = stats["count"] / total_windows
            if stability >= stability_threshold:
                lags = stats["lags"]
                median_lag = int(np.median(lags)) if lags else 0

                consensus.add_edge(
                    u,
                    v,
                    stability=stability,
                    optimal_lag=median_lag,
                    status="stable_temporal",
                )

        if self.strict_dag and not nx.is_directed_acyclic_graph(consensus):
            consensus = self._break_cycles(consensus)

        return consensus

    def _align_temporal_data(
        self, df: pd.DataFrame, dag: nx.DiGraph, target: str
    ) -> pd.DataFrame:
        """Shifts predecessor columns by their optimal lag.

        This ensures that estimators see aligned cause-effect rows, effectively
        mapping X at (t - lag) to Y at (t) on the same dataframe row.

        Args:
            df (pd.DataFrame): Dataframe to align.
            dag (nx.DiGraph): The causal graph containing the optimal lags.
            target (str): The outcome variable.

        Returns:
            pd.DataFrame: The temporally aligned dataframe.
        """
        if target not in dag:
            return df

        df_aligned = df.copy()
        max_shift = 0

        for p in dag.predecessors(target):
            lag = dag[p][target].get("optimal_lag", 0)
            if lag > 0:
                logger.info(
                    "aligning_temporal_cause", cause=p, target=target, lag=lag
                )
                df_aligned[p] = df_aligned[p].shift(lag)
                max_shift = max(max_shift, lag)

        if max_shift > 0:
            df_aligned = df_aligned.iloc[max_shift:].reset_index(drop=True)

        return df_aligned

    def _estimate_effects(
        self,
        df: pd.DataFrame,
        dag: nx.DiGraph,
        target_outcome: str,
        embedding_col: Optional[str],
    ) -> List[Any]:
        """Routes to the optimal estimator and computes causal effects.

        Args:
            df (pd.DataFrame): The aligned dataframe.
            dag (nx.DiGraph): The consensus causal graph.
            target_outcome (str): The outcome variable.
            embedding_col (Optional[str]): Column containing high-dimensional embeddings.

        Returns:
            List[Any]: A list of estimation result objects (HeterogeneousEstimateResult
                or StandardEstimateResult).
        """
        results = []
        if target_outcome not in dag:
            return results

        predecessors = list(dag.predecessors(target_outcome))
        if not predecessors:
            return results

        if embedding_col and embedding_col in df.columns:
            logger.info(
                "routing_to_heterogeneous_estimator", confounder=embedding_col
            )
            estimator = EmbeddingConfounderEstimator(discrete_treatment=False)

            for treatment in predecessors:
                res = estimator.estimate_effect(
                    df=df,
                    treatment=treatment,
                    outcome=target_outcome,
                    embedding_col=embedding_col,
                    dag=dag,
                )
                if res and res.refutation_passed:
                    results.append(res)
        else:
            logger.info("routing_to_standard_dowhy_estimator")
            estimator = DowhyEstimator(strict_dag=True)
            for treatment in predecessors:
                res = estimator.estimate_effect(
                    df=df, dag=dag, treatment=treatment, outcome=target_outcome
                )
                if res and res.refutation_passed:
                    results.append(res)

        return results

    def _break_cycles(self, dag: nx.DiGraph) -> nx.DiGraph:
        """Removes the least stable edges to break graph cycles.

        Args:
            dag (nx.DiGraph): A directed graph that might contain cycles.

        Returns:
            nx.DiGraph: A strictly Directed Acyclic Graph (DAG).
        """
        clean_dag = dag.copy()
        while not nx.is_directed_acyclic_graph(clean_dag):
            try:
                cycle = nx.find_cycle(clean_dag)
                weakest = min(
                    cycle,
                    key=lambda e: clean_dag[e[0]][e[1]].get("stability", 1.0),
                )
                clean_dag.remove_edge(*weakest)
            except nx.NetworkXNoCycle:
                break
        return clean_dag
