"""An offline retrieval benchmark shares source identities with the ESKG oracle."""

import sys

import pytest
from retrieval_benchmark import (
    evaluate,
    knowledge_metrics,
    load_benchmark,
    recall_at,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_recall_counts_unique_relevant_documents_and_rejects_invalid_qrels() -> (
    None
):
    assert recall_at(["a", "a", "b"], {"a", "c"}, 5) == 0.5
    assert recall_at(["c", "a", "b"], {"a", "c"}, 2) == 1.0
    with pytest.raises(ValueError):
        recall_at([], set(), 5)


@pytest.mark.anyio
async def test_cached_models_and_graph_share_the_attributed_retail_source() -> (
    None
):
    benchmark = load_benchmark()
    assert benchmark.recognition.model.startswith("local-eru:")
    assert benchmark.knowledge is not None
    assert set(benchmark.knowledge.documents) == {
        document.id for document in benchmark.documents
    }
    for document in benchmark.documents:
        for fact in benchmark.knowledge.documents[document.id]:
            assert (
                fact.subject in document.text and fact.object in document.text
            )
    report = await evaluate(benchmark)
    assert report["queries"] >= 6
    for mode in ("lexical", "semantic", "hybrid", "hipporag2"):
        assert 0 <= report[mode] <= 1
    print(report)
    print("Eru", knowledge_metrics(benchmark, benchmark.knowledge))
    if benchmark.knowledge_baseline is not None:
        print(
            "Eru before",
            knowledge_metrics(benchmark, benchmark.knowledge_baseline),
        )
    print("Qwen reranker", await evaluate(benchmark, "qwen"))
    assert report["hipporag2"] + 1e-9 >= report["semantic"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-s"]))
