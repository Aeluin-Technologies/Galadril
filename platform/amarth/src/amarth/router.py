"""Orchestrates multimodal, time-windowed causal inference for ESKG data."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypedDict

import networkx as nx
import numpy as np
import pandas as pd
import structlog
from sklearn.decomposition import PCA

from amarth.discovery import DiscoveryMethod, discover_graph
from amarth.estimation.dowhy import DowhyEstimator
from amarth.observations import (
    CausalLink,
    ObservationWindow,
    prepare_observation_window,
)

logger = structlog.get_logger(__name__)


class _InferredParameters(TypedDict):
    window_size: str | None
    tau_max: int
    stability: float
    min_window_samples: int


@dataclass(slots=True)
class _EdgeStatistics:
    count: int = 0
    lags: list[float] = field(default_factory=list)
    effects: list[float] = field(default_factory=list)
    p_values: list[float] = field(default_factory=list)
    q_values: list[float] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    method: str = "tigramite.pcmci"


class AmarthRouter:
    """Executes discovery, effect estimation, and causal-link enrichment."""

    def __init__(
        self,
        strict_dag: bool = True,
        *,
        max_pcmci_variables: int = 32,
        max_analysis_windows: int = 16,
    ) -> None:
        if max_pcmci_variables < 2:
            raise ValueError("max_pcmci_variables must be at least two")
        if max_analysis_windows < 1:
            raise ValueError("max_analysis_windows must be positive")
        self.strict_dag = strict_dag
        self.max_pcmci_variables = max_pcmci_variables
        self.max_analysis_windows = max_analysis_windows

    def analyze_observation_window(
        self,
        window: ObservationWindow,
        target_outcome: str,
        *,
        prior_graph: nx.DiGraph | None = None,
        analysis_window_size: str | None = None,
    ) -> dict[str, Any]:
        """Natively analyzes heterogeneous observations from one ESKG window."""
        prepared = prepare_observation_window(window)
        return self.analyze(
            df=prepared.frame,
            target_outcome=target_outcome,
            time_col="timestamp",
            embedding_cols=prepared.vector_columns,
            prior_graph=prior_graph,
            window_size=analysis_window_size,
            sampling_interval_seconds=prepared.sampling_interval_seconds,
            feature_node_ids=prepared.feature_node_ids,
        )

    def analyze(
        self,
        df: pd.DataFrame,
        target_outcome: str,
        time_col: str | None = None,
        embedding_col: str | None = None,
        prior_graph: nx.DiGraph | None = None,
        window_size: str | None = None,
        *,
        embedding_cols: Sequence[str] | None = None,
        sampling_interval_seconds: float | None = None,
        feature_node_ids: dict[str, tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        """Runs causal discovery over scalar, vector, and graph-derived signals."""
        logger.info("amarth_router_started", rows=len(df), cols=len(df.columns))
        vectors = tuple(
            dict.fromkeys(
                (
                    *tuple(embedding_cols or ()),
                    *((embedding_col,) if embedding_col else ()),
                )
            )
        )
        df_clean = self._preprocess_data(df, is_temporal=bool(time_col))
        params = self._infer_dynamic_parameters(
            df_clean, time_col, user_window_size=window_size
        )
        df_features = self._expand_embeddings(df_clean, vectors)
        resolved_target = self._resolve_target(df_features, target_outcome)

        if time_col:
            consensus_dag = self._windowed_temporal_discovery(
                df=df_features,
                time_col=time_col,
                window_size=params["window_size"] or "1h",
                tau_max=params["tau_max"],
                stability_threshold=params["stability"],
                prior_graph=prior_graph,
                target_outcome=resolved_target,
            )
            df_aligned = self._align_temporal_data(
                df_features, consensus_dag, resolved_target
            )
        else:
            consensus_dag = self._static_discovery(df_features, prior_graph)
            df_aligned = df_features

        effects = self._estimate_effects(
            df=df_aligned,
            dag=consensus_dag,
            target_outcome=resolved_target,
        )
        interval_seconds = sampling_interval_seconds or self._infer_interval(
            df_features, time_col
        )
        causal_links = self._build_causal_links(
            consensus_dag,
            sampling_interval_seconds=interval_seconds,
            feature_node_ids=feature_node_ids or {},
        )
        logger.info(
            "amarth_router_completed",
            causal_links=len(causal_links),
            effects=len(effects),
            target=resolved_target,
        )
        return {
            "consensus_dag": consensus_dag,
            "causal_effects": effects,
            "causal_links": causal_links,
            "analysis_frame": df_features,
            "metadata": {
                "samples_processed": len(df_aligned),
                "is_temporal": bool(time_col),
                "used_embeddings": bool(vectors),
                "embedding_columns": vectors,
                "target_outcome": resolved_target,
                "sampling_interval_seconds": interval_seconds,
                "counterfactual_ready": nx.is_directed_acyclic_graph(
                    consensus_dag
                ),
                "inferred_params": params,
            },
        }

    def _infer_dynamic_parameters(
        self,
        df: pd.DataFrame,
        time_col: str | None,
        user_window_size: str | None = None,
    ) -> _InferredParameters:
        """Sizes PCMCI lag and stability parameters from the observed cadence."""
        sample_count = len(df)
        if not time_col:
            return {
                "window_size": None,
                "tau_max": 0,
                "stability": 0.5,
                "min_window_samples": max(1, sample_count // 3),
            }

        times = pd.to_datetime(df[time_col], utc=True)
        duration = times.max() - times.min()
        if duration <= pd.Timedelta(days=1):
            default_window = "1h"
            tau_max = min(10, sample_count // 20)
        elif duration <= pd.Timedelta(days=30):
            default_window = "1D"
            tau_max = min(12, sample_count // 20)
        elif duration <= pd.Timedelta(days=365):
            default_window = "30D"
            tau_max = min(7, sample_count // 20)
        else:
            default_window = "365D"
            tau_max = min(5, sample_count // 20)

        analysis_window = user_window_size or default_window
        try:
            estimated_windows = max(
                1, int(duration / pd.to_timedelta(analysis_window))
            )
        except (TypeError, ValueError):
            estimated_windows = 1
        return {
            "window_size": analysis_window,
            "tau_max": max(1, tau_max),
            "stability": max(0.25, 1.0 / estimated_windows),
            "min_window_samples": max(30, (max(1, tau_max) * 3) + 1),
        }

    def _preprocess_data(
        self, df: pd.DataFrame, is_temporal: bool
    ) -> pd.DataFrame:
        """Imputes sparse scalar and vector observations without changing order."""
        clean = df.copy().dropna(axis=1, how="all")
        numeric_columns = clean.select_dtypes(include=[np.number]).columns
        if is_temporal:
            clean[numeric_columns] = (
                clean[numeric_columns]
                .interpolate(method="linear")
                .bfill()
                .ffill()
            )
        else:
            clean[numeric_columns] = clean[numeric_columns].fillna(
                clean[numeric_columns].median()
            )

        for column in clean.columns:
            if (
                clean[column]
                .map(lambda value: isinstance(value, (list, tuple, np.ndarray)))
                .any()
            ):
                clean[column] = clean[column].ffill().bfill()
        return clean

    def _expand_embeddings(
        self, df: pd.DataFrame, embedding_columns: Sequence[str]
    ) -> pd.DataFrame:
        """Projects every dense modality into bounded scalar components for PCMCI."""
        expanded = df.copy()
        for column in embedding_columns:
            if column not in expanded.columns:
                continue
            vectors = expanded[column]
            first = next(
                (
                    value
                    for value in vectors
                    if isinstance(value, (list, tuple, np.ndarray))
                ),
                None,
            )
            if first is None:
                expanded = expanded.drop(columns=[column])
                continue
            dimension = len(first)
            matrix = np.empty((len(expanded), dimension), dtype=np.float64)
            for row, value in enumerate(vectors):
                vector = np.asarray(value, dtype=np.float64)
                if vector.shape != (dimension,):
                    raise ValueError(
                        f"embedding '{column}' has inconsistent dimensions"
                    )
                matrix[row] = vector

            component_count = min(dimension, max(1, len(expanded) // 10), 16)
            if component_count < dimension:
                matrix = PCA(
                    n_components=component_count,
                    svd_solver="randomized",
                    random_state=0,
                ).fit_transform(matrix)
            expanded = expanded.drop(columns=[column])
            for component in range(matrix.shape[1]):
                expanded[f"{column}.pc{component}"] = matrix[:, component]
            logger.info(
                "embedding_projected",
                feature=column,
                original_dimensions=dimension,
                retained_dimensions=matrix.shape[1],
            )
        return expanded

    def _windowed_temporal_discovery(
        self,
        df: pd.DataFrame,
        time_col: str,
        window_size: str,
        tau_max: int,
        stability_threshold: float,
        prior_graph: nx.DiGraph | None,
        target_outcome: str,
    ) -> nx.DiGraph:
        """Aggregates Tigramite results across sufficiently sampled time windows."""
        time_series = df.set_index(time_col).sort_index()
        scalar = time_series.select_dtypes(include=[np.number])
        scalar = self._select_pcmci_features(
            scalar, target_outcome, prior_graph
        )
        min_samples = max(30, (tau_max * 3) + 1)
        candidate_windows = [
            group
            for _, group in scalar.resample(window_size)
            if len(group) >= min_samples
        ]
        if not candidate_windows and len(scalar) >= min_samples:
            candidate_windows = [scalar]
        if len(candidate_windows) > self.max_analysis_windows:
            indices = np.linspace(
                0,
                len(candidate_windows) - 1,
                num=self.max_analysis_windows,
                dtype=np.int64,
            )
            candidate_windows = [
                candidate_windows[int(index)] for index in indices
            ]

        dags = [
            discover_graph(
                window,
                method=DiscoveryMethod.PCMCI,
                tau_max=tau_max,
                pc_alpha=0.2,
                mci_alpha=0.05,
            )
            for window in candidate_windows
        ]
        consensus = self._aggregate_dags(dags, stability_threshold)
        if prior_graph is not None:
            for source, target in consensus.edges:
                consensus.edges[source, target]["prior_supported"] = (
                    prior_graph.has_edge(source, target)
                )
        return consensus

    def _select_pcmci_features(
        self,
        scalar: pd.DataFrame,
        target_outcome: str,
        prior_graph: nx.DiGraph | None,
    ) -> pd.DataFrame:
        """Applies a deterministic feature budget around the requested outcome."""
        if len(scalar.columns) <= self.max_pcmci_variables:
            return scalar

        selected: list[str] = []
        if target_outcome in scalar.columns:
            selected.append(target_outcome)
        if prior_graph is not None and target_outcome in prior_graph:
            for predecessor in prior_graph.predecessors(target_outcome):
                name = str(predecessor)
                if name in scalar.columns and name not in selected:
                    selected.append(name)
        for column in scalar.columns:
            name = str(column)
            if name not in selected:
                selected.append(name)
            if len(selected) >= self.max_pcmci_variables:
                break
        retained = selected[: self.max_pcmci_variables]
        logger.warning(
            "pcmci_feature_budget_applied",
            available=len(scalar.columns),
            retained=len(retained),
            target=target_outcome,
        )
        return scalar.loc[:, retained]

    def _static_discovery(
        self, df: pd.DataFrame, prior_graph: nx.DiGraph | None
    ) -> nx.DiGraph:
        """Builds a static causal graph only from explicit ESKG priors."""
        if prior_graph is None:
            raise ValueError(
                "static causal analysis requires prior_graph; use timestamped "
                "observations for PCMCI discovery"
            )
        scalar = df.select_dtypes(include=[np.number])
        variables = frozenset(str(column) for column in scalar.columns)
        graph = nx.DiGraph()
        graph.add_nodes_from(variables)
        for source, target, data in prior_graph.edges(data=True):
            source_name = str(source)
            target_name = str(target)
            if source_name not in variables or target_name not in variables:
                continue
            attributes = dict(data)
            attributes["prior_supported"] = True
            attributes.setdefault("method", "eskg.prior")
            attributes.setdefault("status", "prior_confirmed")
            attributes.setdefault("confidence_score", 1.0)
            graph.add_edge(source_name, target_name, **attributes)
        if self.strict_dag and not nx.is_directed_acyclic_graph(graph):
            return self._break_cycles(graph)
        return graph

    def _aggregate_dags(
        self, dags: list[nx.DiGraph], stability_threshold: float
    ) -> nx.DiGraph:
        """Combines window evidence into calibrated lag and confidence values."""
        if not dags:
            return nx.DiGraph()
        stats: dict[tuple[str, str], _EdgeStatistics] = {}
        for dag in dags:
            for source, target, data in dag.edges(data=True):
                edge = stats.setdefault(
                    (str(source), str(target)),
                    _EdgeStatistics(
                        method=str(data.get("method", "tigramite.pcmci"))
                    ),
                )
                edge.count += 1
                for values, data_key in (
                    (edge.lags, "optimal_lag"),
                    (edge.effects, "effect_size"),
                    (edge.p_values, "p_value"),
                    (edge.q_values, "q_value"),
                    (edge.confidences, "confidence_score"),
                ):
                    value = data.get(data_key)
                    if isinstance(value, (int, float)):
                        values.append(float(value))

        consensus = nx.DiGraph()
        consensus.add_nodes_from(dags[0].nodes)
        for (source, target), edge in stats.items():
            stability = edge.count / len(dags)
            if stability < stability_threshold:
                continue
            base_confidence = (
                float(np.median(edge.confidences))
                if edge.confidences
                else stability
            )
            consensus.add_edge(
                source,
                target,
                stability=stability,
                optimal_lag=self._median_int(edge.lags),
                effect_size=self._median_float(edge.effects),
                weight=self._median_float(edge.effects),
                p_value=self._median_optional(edge.p_values),
                q_value=self._median_optional(edge.q_values),
                confidence_score=float(
                    np.clip(base_confidence * stability, 0.0, 1.0)
                ),
                method=edge.method,
                status="stable_temporal",
            )
        if self.strict_dag and not nx.is_directed_acyclic_graph(consensus):
            return self._break_cycles(consensus)
        return consensus

    @staticmethod
    def _median_int(values: list[float]) -> int:
        return int(np.median(values)) if values else 0

    @staticmethod
    def _median_float(values: list[float]) -> float:
        return float(np.median(values)) if values else 0.0

    @staticmethod
    def _median_optional(
        values: list[float],
    ) -> float | None:
        return float(np.median(values)) if values else None

    def _align_temporal_data(
        self, df: pd.DataFrame, dag: nx.DiGraph, target: str
    ) -> pd.DataFrame:
        """Aligns lagged causes with the target before DoWhy estimation."""
        if target not in dag:
            return df
        aligned = df.copy()
        max_shift = 0
        for predecessor in dag.predecessors(target):
            lag = int(dag.edges[predecessor, target].get("optimal_lag", 0))
            if lag > 0:
                aligned[predecessor] = aligned[predecessor].shift(lag)
                max_shift = max(max_shift, lag)
        if max_shift > 0:
            return aligned.iloc[max_shift:].reset_index(drop=True)
        return aligned

    def _estimate_effects(
        self, df: pd.DataFrame, dag: nx.DiGraph, target_outcome: str
    ) -> list[Any]:
        """Uses DoWhy to validate each direct antecedent of the requested outcome."""
        if target_outcome not in dag:
            return []
        estimator = DowhyEstimator(strict_dag=True)
        results: list[Any] = []
        numeric = df.select_dtypes(include=[np.number])
        for treatment in dag.predecessors(target_outcome):
            try:
                result = estimator.estimate_effect(
                    df=numeric,
                    dag=dag,
                    treatment=str(treatment),
                    outcome=target_outcome,
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "causal_effect_estimation_failed",
                    treatment=treatment,
                    outcome=target_outcome,
                    error=str(exc),
                )
                continue
            if result is not None and result.refutation_passed:
                results.append(result)
        return results

    def _build_causal_links(
        self,
        dag: nx.DiGraph,
        *,
        sampling_interval_seconds: float,
        feature_node_ids: dict[str, tuple[str, ...]],
    ) -> list[CausalLink]:
        """Converts discovery edges into graph-store-ready causal contracts."""
        links: list[CausalLink] = []
        for source, target, data in dag.edges(data=True):
            lag_steps = int(data.get("optimal_lag", 0))
            source_name = str(source)
            target_name = str(target)
            links.append(
                CausalLink(
                    source_feature=source_name,
                    target_feature=target_name,
                    confidence_score=float(
                        np.clip(data.get("confidence_score", 0.0), 0.0, 1.0)
                    ),
                    time_lag_seconds=lag_steps * sampling_interval_seconds,
                    lag_steps=lag_steps,
                    effect_size=float(
                        data.get("effect_size", data.get("weight", 0.0))
                    ),
                    p_value=self._optional_probability(data.get("p_value")),
                    q_value=self._optional_probability(data.get("q_value")),
                    stability=float(
                        np.clip(data.get("stability", 1.0), 0.0, 1.0)
                    ),
                    method=str(data.get("method", "eskg.prior")),
                    source_node_ids=feature_node_ids.get(
                        self._feature_family(source_name), ()
                    ),
                    target_node_ids=feature_node_ids.get(
                        self._feature_family(target_name), ()
                    ),
                    supports_counterfactual=nx.is_directed_acyclic_graph(dag),
                )
            )
        return links

    @staticmethod
    def _feature_family(feature: str) -> str:
        return feature.split(".", 1)[0]

    @staticmethod
    def _optional_probability(value: object) -> float | None:
        if isinstance(value, (int, float)) and np.isfinite(value):
            return float(np.clip(value, 0.0, 1.0))
        return None

    @staticmethod
    def _resolve_target(df: pd.DataFrame, requested: str) -> str:
        if requested in df.columns:
            return requested
        candidates = [
            column
            for column in df.columns
            if column.startswith(f"{requested}.") and ".pc" not in column
        ]
        if len(candidates) == 1:
            return str(candidates[0])
        raise ValueError(
            f"target outcome '{requested}' is absent or ambiguous; "
            f"matching features: {candidates}"
        )

    @staticmethod
    def _infer_interval(df: pd.DataFrame, time_col: str | None) -> float:
        if not time_col or len(df) < 2:
            return 0.0
        timestamps = pd.to_datetime(df[time_col], utc=True).sort_values()
        intervals = timestamps.diff().dropna().dt.total_seconds()
        return float(intervals.median()) if not intervals.empty else 0.0

    def _break_cycles(self, dag: nx.DiGraph) -> nx.DiGraph:
        """Removes the least confident edge until DoWhy receives a valid DAG."""
        clean = dag.copy()
        while not nx.is_directed_acyclic_graph(clean):
            cycle = nx.find_cycle(clean)
            weakest = min(
                cycle,
                key=lambda edge: clean.edges[edge].get("confidence_score", 0.0),
            )
            clean.remove_edge(*weakest)
        return clean
