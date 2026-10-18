"""Qwen relevance uses the trained yes/no logits instead of generated prose."""

import math
import sys

import pytest
from galadril_inference.models.qwen_reranking import QwenRerankerModel


def test_yes_no_logits_use_a_stable_two_class_normalization() -> None:
    assert QwenRerankerModel.relevance(1.0, 1.0) == 0.5
    assert QwenRerankerModel.relevance(1000.0, -1000.0) == 1.0
    assert QwenRerankerModel.relevance(-1000.0, 1000.0) == 0.0
    for invalid in (math.inf, math.nan, -math.inf):
        with pytest.raises(ValueError):
            QwenRerankerModel.relevance(invalid, 0.0)


def test_prompt_budget_never_truncates_the_query_or_the_judgement_suffix() -> (
    None
):
    assert (
        QwenRerankerModel.document_budget(4096, prefix=20, query=100, suffix=30)
        == 3945
    )
    with pytest.raises(ValueError):
        QwenRerankerModel.document_budget(128, prefix=20, query=100, suffix=30)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
