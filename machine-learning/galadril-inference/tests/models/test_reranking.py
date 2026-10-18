"""Reranking logits remain bounded relevance scores, not correctness probabilities."""

import sys

import numpy as np
import pytest
from galadril_inference.models.reranking import BgeRerankerModel


def test_reranking_scores_are_stable_for_extreme_logits() -> None:
    scores = BgeRerankerModel.normalize_logits(
        np.asarray([-1000.0, 0.0, 1000.0], dtype=np.float32), expected=3
    )
    assert scores[0] < 0.001 and scores[1] == 0.5 and scores[2] > 0.999


@pytest.mark.parametrize(
    "values",
    [
        np.asarray([float("nan")]),
        np.asarray([float("inf")]),
        np.asarray([1.0, 2.0]),
    ],
)
def test_invalid_reranker_outputs_cannot_silently_reorder_evidence(
    values: np.ndarray,
) -> None:
    with pytest.raises(ValueError):
        BgeRerankerModel.normalize_logits(values, expected=1)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
