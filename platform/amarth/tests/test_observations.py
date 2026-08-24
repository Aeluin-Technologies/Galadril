"""Tests for heterogeneous ESKG observation-window preparation."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from amarth import (
    GraphRelationshipObservation,
    Observation,
    ObservationWindow,
    prepare_observation_window,
)
from pydantic import ValidationError


def test_prepare_observation_window_joins_multimodal_features() -> None:
    """Preserves scalar, vector, ontology, and derived relationship evidence."""
    start = datetime(2026, 8, 24, tzinfo=UTC)
    window = ObservationWindow(
        start=start,
        end=start + timedelta(seconds=8),
        bucket=timedelta(seconds=1),
        observations=(
            Observation(
                observation_id="vision-1",
                graph_node_id="face-event",
                observed_at=start,
                observation_type="FacialExpressionShift",
                scalar_values={"confidence": 0.94},
                embeddings={"facial_embedding": (0.1, 0.2, 0.3)},
            ),
            Observation(
                observation_id="text-1",
                graph_node_id="text-state",
                observed_at=start + timedelta(seconds=3),
                observation_type="TextSentimentChange",
                scalar_values={"sentiment": -0.8},
                embeddings={"text_embedding": (0.4, 0.5)},
            ),
        ),
        relationships=(
            GraphRelationshipObservation(
                source_node_id="face-event",
                target_node_id="text-state",
                relationship_type="TRIGGERS",
                observed_at=start + timedelta(seconds=3),
            ),
        ),
    )

    prepared = prepare_observation_window(window)

    assert len(prepared.frame) == 9
    assert prepared.sampling_interval_seconds == 1.0
    assert {
        "FacialExpressionShift.confidence",
        "FacialExpressionShift.facial_embedding",
        "TextSentimentChange.sentiment",
        "TextSentimentChange.text_embedding",
        "relationship.TRIGGERS",
    }.issubset(prepared.frame.columns)
    assert np.array_equal(
        prepared.frame.loc[0, "FacialExpressionShift.facial_embedding"],
        np.asarray((0.1, 0.2, 0.3)),
    )
    assert prepared.feature_node_ids["FacialExpressionShift"] == ("face-event",)
    assert prepared.feature_node_ids["TextSentimentChange"] == ("text-state",)


def test_observation_window_rejects_out_of_window_observation() -> None:
    """Rejects evidence that would silently contaminate a causal time slice."""
    start = datetime(2026, 8, 24, tzinfo=UTC)

    try:
        ObservationWindow(
            start=start,
            end=start + timedelta(seconds=3),
            bucket=timedelta(seconds=1),
            observations=(
                Observation(
                    observation_id="late",
                    graph_node_id="node-late",
                    observed_at=start + timedelta(seconds=4),
                    observation_type="LateObservation",
                ),
            ),
        )
    except ValueError as exc:
        assert "outside the observation window" in str(exc)
    else:
        raise AssertionError("Expected out-of-window evidence to be rejected")


@pytest.mark.parametrize(
    ("scalar_values", "embeddings", "message"),
    [
        (
            {"score": float("nan")},
            {},
            "scalar observation values must be finite",
        ),
        ({}, {"vector": ()}, "must not be empty"),
        ({}, {"vector": (1.0, float("inf"))}, "must contain finite values"),
    ],
)
def test_observation_rejects_invalid_numerical_payloads(
    scalar_values: dict[str, float],
    embeddings: dict[str, tuple[float, ...]],
    message: str,
) -> None:
    """Rejects values that would corrupt downstream numerical inference."""
    with pytest.raises(ValidationError, match=message):
        Observation(
            observation_id="invalid",
            graph_node_id="node",
            observed_at=datetime(2026, 8, 24, tzinfo=UTC),
            observation_type="Signal",
            scalar_values=scalar_values,
            embeddings=embeddings,
        )


@pytest.mark.parametrize(
    ("start", "end", "bucket", "message"),
    [
        (
            datetime(2026, 8, 24),
            datetime(2026, 8, 25),
            timedelta(seconds=1),
            "timezone-aware",
        ),
        (
            datetime(2026, 8, 24, tzinfo=UTC),
            datetime(2026, 8, 24, tzinfo=UTC),
            timedelta(seconds=1),
            "end must follow start",
        ),
        (
            datetime(2026, 8, 24, tzinfo=UTC),
            datetime(2026, 8, 25, tzinfo=UTC),
            timedelta(0),
            "bucket must be positive",
        ),
    ],
)
def test_observation_window_rejects_invalid_bounds(
    start: datetime, end: datetime, bucket: timedelta, message: str
) -> None:
    """Rejects invalid time bounds and sampling cadence."""
    with pytest.raises(ValidationError, match=message):
        ObservationWindow(
            start=start,
            end=end,
            bucket=bucket,
            observations=(),
        )


def test_observation_window_rejects_out_of_window_relationship() -> None:
    """Rejects relationship evidence outside the causal observation window."""
    start = datetime(2026, 8, 24, tzinfo=UTC)
    relationship = GraphRelationshipObservation(
        source_node_id="source",
        target_node_id="target",
        relationship_type="TRIGGERS",
        observed_at=start + timedelta(seconds=2),
    )
    with pytest.raises(
        ValidationError, match="relationship 'TRIGGERS'.*outside"
    ):
        ObservationWindow(
            start=start,
            end=start + timedelta(seconds=1),
            bucket=timedelta(seconds=1),
            observations=(),
            relationships=(relationship,),
        )


def test_prepare_observation_window_rejects_embedding_shape_drift() -> None:
    """Rejects one feature family changing vector dimensions over time."""
    start = datetime(2026, 8, 24, tzinfo=UTC)
    observations = (
        Observation(
            observation_id="one",
            graph_node_id="node-one",
            observed_at=start,
            observation_type="Signal",
            embeddings={"vector": (1.0, 2.0)},
        ),
        Observation(
            observation_id="two",
            graph_node_id="node-two",
            observed_at=start + timedelta(seconds=1),
            observation_type="Signal",
            embeddings={"vector": (1.0,)},
        ),
    )
    window = ObservationWindow(
        start=start,
        end=start + timedelta(seconds=1),
        bucket=timedelta(seconds=1),
        observations=observations,
    )

    with pytest.raises(ValueError, match="changed dimensions"):
        prepare_observation_window(window)
