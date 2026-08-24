"""DoWhy graphical causal model adapter for what-if simulation."""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx
import pandas as pd
from dowhy import gcm


@dataclass(frozen=True, slots=True)
class WhatIfSimulator:
    """Runs interventions and counterfactuals against a fitted continuous SCM."""

    _model: gcm.StructuralCausalModel

    @classmethod
    def fit(cls, frame: pd.DataFrame, dag: nx.DiGraph) -> WhatIfSimulator:
        """Fits additive-noise mechanisms for a discovered causal DAG."""
        if not nx.is_directed_acyclic_graph(dag):
            raise ValueError("counterfactual simulation requires a DAG")
        missing = set(dag.nodes).difference(frame.columns)
        if missing:
            raise ValueError(
                f"causal graph variables are absent from data: {sorted(missing)}"
            )

        model = gcm.StructuralCausalModel(dag.copy())
        for node in nx.topological_sort(dag):
            if dag.in_degree(node) == 0:
                model.set_causal_mechanism(node, gcm.EmpiricalDistribution())
            else:
                model.set_causal_mechanism(
                    node,
                    gcm.AdditiveNoiseModel(gcm.ml.create_linear_regressor()),
                )
        gcm.fit(model, frame.loc[:, list(dag.nodes)])
        return cls(_model=model)

    def intervene(
        self, interventions: dict[str, float], sample_count: int = 1_000
    ) -> pd.DataFrame:
        """Generates downstream states under atomic antecedent interventions."""
        if sample_count <= 0:
            raise ValueError("sample_count must be positive")
        unknown = set(interventions).difference(self._model.graph.nodes)
        if unknown:
            raise ValueError(f"unknown intervention nodes: {sorted(unknown)}")

        intervention_functions = {
            node: (lambda _current, value=value: value)
            for node, value in interventions.items()
        }
        return gcm.interventional_samples(
            self._model,
            intervention_functions,
            num_samples_to_draw=sample_count,
        )

    def counterfactual(
        self,
        observation: pd.DataFrame,
        interventions: dict[str, float],
    ) -> pd.DataFrame:
        """Replays an observed state after changing or removing an antecedent."""
        intervention_functions = {
            node: (lambda _current, value=value: value)
            for node, value in interventions.items()
        }
        return gcm.counterfactual_samples(
            self._model,
            intervention_functions,
            observed_data=observation,
        )

    def remove_antecedent(
        self,
        frame: pd.DataFrame,
        antecedent: str,
        sample_count: int = 1_000,
    ) -> pd.DataFrame:
        """Refits the downstream SCM after structurally deleting an antecedent."""
        if antecedent not in self._model.graph:
            raise ValueError(f"unknown antecedent node: {antecedent}")
        reduced_graph = self._model.graph.copy()
        reduced_graph.remove_node(antecedent)
        reduced_frame = frame.drop(columns=[antecedent])
        reduced_model = WhatIfSimulator.fit(reduced_frame, reduced_graph)
        return reduced_model.intervene({}, sample_count=sample_count)
