"""HotpotQA distractors measure retrieval without answer generation or model downloads."""

import json

import pytest
from hotpot_benchmark import evaluate_hotpot, load_hotpot, paired_comparison


def test_hotpot_subset_is_balanced_and_has_complete_supporting_evidence() -> (
    None
):
    data = load_hotpot()
    assert len(data.questions) == 40
    assert sum(question.kind == "bridge" for question in data.questions) == 20
    assert (
        sum(question.kind == "comparison" for question in data.questions) == 20
    )
    for question in data.questions:
        assert len(question.context) == 10
        assert len({title for title, _ in question.supporting_facts}) == 2
        for title, sentence in question.supporting_facts:
            assert question.context[title][sentence]


def test_hotpot_cached_models_report_recall_and_joint_support_at_five() -> None:
    report = evaluate_hotpot(load_hotpot())
    print(json.dumps(report, sort_keys=True))
    assert report["queries"] == 40
    assert set(report["recall_at_5"]) == {
        "lexical",
        "semantic",
        "hybrid",
        "hybrid_bge",
    }
    for mode, value in report["recall_at_5"].items():
        assert 0 <= report["both_supports_at_5"][mode] <= value <= 1
    assert report["recall_at_5"]["semantic"] >= 0.8875 - 1e-9
    assert report["recall_at_5"]["hybrid_bge"] >= 0.9875 - 1e-9
    assert report["both_supports_at_5"]["hybrid_bge"] >= 0.975 - 1e-9


def test_paired_hypothesis_distinguishes_no_change_from_consistent_gain() -> (
    None
):
    unchanged = paired_comparison([0.5] * 40, [0.5] * 40)
    assert unchanged["mean_delta"] == 0
    assert unchanged["ci95"] == (0, 0)
    assert unchanged["one_sided_p"] == 1
    improved = paired_comparison([0.5] * 40, [1.0] * 40)
    assert improved["mean_delta"] == 0.5
    assert improved["ci95"] == (0.5, 0.5)
    assert improved["one_sided_p"] < 0.05
    with pytest.raises(ValueError):
        paired_comparison([0.5] * 40, [1.0] * 39)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-s"]))
