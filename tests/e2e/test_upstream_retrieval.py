"""The upstream graph kernel is a reference, not evidence of full pipeline parity."""

import ast
import hashlib
import json
from pathlib import Path

import pytest
from retrieval_benchmark import load_benchmark
from upstream_retrieval import compare_upstream, rank_upstream


def test_vendored_graph_methods_are_unchanged() -> None:
    root = Path(__file__).parents[2] / "third_party" / "hipporag"
    provenance = json.loads((root / "PROVENANCE.json").read_text())
    tree = ast.parse((root / "graph_reference.py").read_text())
    reference = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "HippoRAGGraphReference"
    )
    for node in reference.body:
        if isinstance(node, ast.FunctionDef):
            assert (
                hashlib.sha256(
                    ast.dump(node, include_attributes=False).encode()
                ).hexdigest()
                == provenance["methods"][node.name]
            )
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            assert (
                hashlib.sha256(
                    ast.dump(node, include_attributes=False).encode()
                ).hexdigest()
                == provenance["functions"][node.name]
            )


def test_upstream_and_eskg_ablation_use_identical_evidence() -> None:
    data = load_benchmark()
    report = compare_upstream(data)
    print(json.dumps(report, sort_keys=True))
    assert set(report["recall_at_5"]) == {
        "upstream_graph",
        "upstream_graph_eskg",
    }
    for value in report["recall_at_5"].values():
        assert 0 <= value <= 1
    assert report["queries"] == len(data.queries)
    assert report["eskg_identity_links"] > 0
    assert report["recall_at_5"]["upstream_graph"] >= 5 / 6 - 1e-9
    assert report["recall_at_5"]["upstream_graph_eskg"] >= 0.875 - 1e-9


def test_authorization_scope_precedes_reference_graph() -> None:
    data = load_benchmark()
    query = data.queries[0]
    allowed = frozenset(
        document.id
        for document in data.documents
        if document.customer_id == data.documents[0].customer_id
    )
    plain = rank_upstream(data, query, allowed=allowed, eskg=False)
    enriched = rank_upstream(data, query, allowed=allowed, eskg=True)
    assert set(plain) == allowed
    assert set(enriched) == allowed
    assert rank_upstream(data, query, allowed=frozenset(), eskg=True) == []


def test_eskg_bridge_rejects_invented_or_wrong_source_identity() -> None:
    data = load_benchmark()
    corrupted = data.model_copy(
        update={
            "documents": (
                data.documents[0].model_copy(update={"source_row": -1}),
                *data.documents[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="ESKG source"):
        compare_upstream(corrupted)


def test_live_eskg_bridge_uses_resolved_entity_ids_and_requires_complete_bindings() -> (
    None
):
    data = load_benchmark()
    bindings = {
        (
            document.customer_id,
            document.source_row,
        ): f"entity-{document.customer_id}"
        for document in data.documents
    }
    report = compare_upstream(data, eskg_bindings=bindings)
    assert report["identity_origin"] == "live_eskg"
    with pytest.raises(ValueError, match="ESKG binding"):
        compare_upstream(data, eskg_bindings={})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-s"]))
