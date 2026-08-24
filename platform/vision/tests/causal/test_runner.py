"""Tests for Vision's ESKG-to-Amarth observation bridge."""

from datetime import UTC, datetime, timedelta

from galadril_vision.causal.runner import _build_observation_window


def test_build_observation_window_keeps_states_embeddings_and_edges() -> None:
    """Builds one typed window without discarding multimodal graph evidence."""
    start = datetime(2026, 8, 24, tzinfo=UTC)
    window = _build_observation_window(
        window_start=start,
        window_end=start + timedelta(seconds=5),
        bucket_seconds=1.0,
        state_rows=(
            (
                start,
                "FacialExpressionShift",
                {"confidence": 0.91},
                "face-node",
                "face-event",
            ),
        ),
        embedding_rows=(
            (
                start,
                "face",
                [0.1, 0.2, 0.3],
                "face-node",
                {
                    "state_type": "FacialExpressionShift",
                    "event_id": "face-event",
                },
                "embedding-1",
            ),
        ),
        relationship_rows=(
            (
                "face-node",
                "text-node",
                "TRIGGERS",
                {"timestamp": (start + timedelta(seconds=2)).isoformat()},
            ),
        ),
    )

    assert len(window.observations) == 2
    assert window.observations[1].embeddings["face_embedding"] == (
        0.1,
        0.2,
        0.3,
    )
    assert window.relationships[0].relationship_type == "TRIGGERS"
