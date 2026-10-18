"""Official engine ablations use cached local models and source-derived ESKG bindings."""

import json

import pytest
from official_memory import compare_memory, load_memory


def test_official_memory_benchmark_uses_at_least_thirty_grounded_questions() -> (
    None
):
    data, fixture = load_memory()
    assert len(fixture.questions) >= 30
    assert fixture.source_sha256 == data.source_sha256
    report = compare_memory(data, fixture)
    print(json.dumps(report, sort_keys=True))
    assert report["queries"] == len(fixture.questions)
    assert set(report["recall_at_5"]) == {
        "dense",
        "official",
        "official_eskg",
        "official_reranked",
        "official_eskg_reranked",
    }
    assert report["eskg_identity_links"] == len(data.documents)
    assert (
        report["recall_at_5"]["official_eskg_reranked"]
        >= report["recall_at_5"]["dense"]
    )
    assert len(report["per_question"]) == len(fixture.questions)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-s"]))
