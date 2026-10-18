"""Chunking measurements retain source evidence rather than changing qrels per policy."""

import json

import pytest
from chunking_benchmark import chunking_report, load_chunking


def test_cached_chunking_preserves_exact_source_spans_and_query_profiles() -> (
    None
):
    data = load_chunking()
    assert set(data.variants) == {
        "fixed",
        "structural",
        "causal_contextual_mean",
    }
    for variant in data.variants.values():
        for chunk in variant.chunks:
            assert chunk.text == data.text[chunk.start : chunk.end]
            assert len(chunk.vector) == 1024
        assert set(variant.query_vectors) == set(data.relevant)
    assert data.variants["fixed"].profile == data.variants["structural"].profile
    assert (
        data.variants["causal_contextual_mean"].profile
        != data.variants["fixed"].profile
    )


def test_chunking_recall_uses_the_same_source_evidence() -> None:
    report = chunking_report(load_chunking())
    print(json.dumps(report, sort_keys=True))
    assert report["queries"] == 8
    assert report["late_chunking_status"] == "experimental_causal_pooling_only"
    for score in report["source_evidence_recall_at_5"].values():
        assert 0 <= score <= 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-s"]))
