"""Causal discovery, estimation, and counterfactual inference library."""

from amarth.discovery import DiscoveryMethod, discover_graph
from amarth.discovery.pcmci import PcmciDiscoverer
from amarth.estimation.dowhy import CausalEstimateResult, DowhyEstimator
from amarth.estimation.heterogeneous import (
    EmbeddingConfounderEstimator,
    HeterogeneousEstimateResult,
)
from amarth.observations import (
    CausalLink,
    GraphRelationshipObservation,
    Observation,
    ObservationWindow,
    PreparedObservationWindow,
    prepare_observation_window,
)
from amarth.router import AmarthRouter
from amarth.simulation import WhatIfSimulator

__all__ = [
    "AmarthRouter",
    "CausalEstimateResult",
    "CausalLink",
    "DiscoveryMethod",
    "DowhyEstimator",
    "EmbeddingConfounderEstimator",
    "GraphRelationshipObservation",
    "HeterogeneousEstimateResult",
    "Observation",
    "ObservationWindow",
    "PcmciDiscoverer",
    "PreparedObservationWindow",
    "WhatIfSimulator",
    "discover_graph",
    "prepare_observation_window",
]
