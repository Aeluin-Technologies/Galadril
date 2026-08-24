"""Tests for DoWhy-backed intervention and counterfactual simulation."""

from unittest.mock import MagicMock, patch

import networkx as nx
import numpy as np
import pandas as pd
import pytest
from amarth.simulation import WhatIfSimulator


def test_intervention_changes_downstream_state() -> None:
    """Uses the fitted SCM to propagate an antecedent intervention."""
    generator = np.random.default_rng(7)
    cause = generator.normal(size=500)
    outcome = (2.0 * cause) + generator.normal(scale=0.05, size=500)
    frame = pd.DataFrame({"cause": cause, "outcome": outcome})
    simulator = WhatIfSimulator.fit(frame, nx.DiGraph((("cause", "outcome"),)))

    baseline = simulator.intervene({}, sample_count=300)
    changed = simulator.intervene({"cause": 3.0}, sample_count=300)
    removed = simulator.remove_antecedent(frame, "cause", sample_count=300)

    assert changed["outcome"].mean() > baseline["outcome"].mean() + 4.0
    assert "cause" not in removed.columns
    assert "outcome" in removed.columns


def test_fit_rejects_cycles_and_missing_variables() -> None:
    """Rejects graph structures that cannot define a valid SCM."""
    cyclic = nx.DiGraph([("a", "b"), ("b", "a")])
    with pytest.raises(ValueError, match="requires a DAG"):
        WhatIfSimulator.fit(pd.DataFrame({"a": [1], "b": [2]}), cyclic)

    with pytest.raises(ValueError, match="variables are absent"):
        WhatIfSimulator.fit(pd.DataFrame({"a": [1]}), nx.DiGraph([("a", "b")]))


def test_fit_assigns_root_and_child_mechanisms() -> None:
    """Assigns distinct mechanisms to roots and causally dependent nodes."""
    graph = nx.DiGraph([("cause", "outcome")])
    frame = pd.DataFrame({"cause": [1.0, 2.0], "outcome": [2.0, 4.0]})
    model = MagicMock()
    model.graph = graph
    root_mechanism = object()
    child_mechanism = object()
    with (
        patch(
            "amarth.simulation.gcm.StructuralCausalModel", return_value=model
        ),
        patch(
            "amarth.simulation.gcm.EmpiricalDistribution",
            return_value=root_mechanism,
        ),
        patch(
            "amarth.simulation.gcm.AdditiveNoiseModel",
            return_value=child_mechanism,
        ),
        patch(
            "amarth.simulation.gcm.ml.create_linear_regressor",
            return_value=object(),
        ),
        patch("amarth.simulation.gcm.fit") as fit,
    ):
        simulator = WhatIfSimulator.fit(frame, graph)

    assert simulator._model is model
    assert model.set_causal_mechanism.call_args_list[0].args == (
        "cause",
        root_mechanism,
    )
    assert model.set_causal_mechanism.call_args_list[1].args == (
        "outcome",
        child_mechanism,
    )
    fit.assert_called_once()


def test_intervention_and_counterfactual_delegate_atomic_functions() -> None:
    """Builds constant intervention functions for both simulation modes."""
    model = MagicMock()
    model.graph = nx.DiGraph([("cause", "outcome")])
    simulator = WhatIfSimulator(_model=model)
    expected = pd.DataFrame({"outcome": [4.0]})
    with (
        patch(
            "amarth.simulation.gcm.interventional_samples",
            return_value=expected,
        ) as intervene,
        patch(
            "amarth.simulation.gcm.counterfactual_samples",
            return_value=expected,
        ) as counterfactual,
    ):
        assert simulator.intervene({"cause": 2.0}, sample_count=7) is expected
        observed = pd.DataFrame({"cause": [1.0], "outcome": [2.0]})
        assert simulator.counterfactual(observed, {"cause": 3.0}) is expected

    intervention = intervene.call_args.args[1]["cause"]
    counterfactual_intervention = counterfactual.call_args.args[1]["cause"]
    assert intervention(99.0) == 2.0
    assert counterfactual_intervention(99.0) == 3.0


def test_intervention_and_removal_reject_unknown_or_invalid_input() -> None:
    """Validates intervention counts and node identities before invoking DoWhy."""
    model = MagicMock()
    model.graph = nx.DiGraph([("cause", "outcome")])
    simulator = WhatIfSimulator(_model=model)
    with pytest.raises(ValueError, match="sample_count must be positive"):
        simulator.intervene({}, sample_count=0)
    with pytest.raises(ValueError, match="unknown intervention nodes"):
        simulator.intervene({"missing": 1.0})
    with pytest.raises(ValueError, match="unknown antecedent node"):
        simulator.remove_antecedent(pd.DataFrame(), "missing")
