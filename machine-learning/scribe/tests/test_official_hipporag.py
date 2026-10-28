"""Exercise the complete upstream index and retriever with local inference adapters."""

import hashlib
import json
import logging
from pathlib import Path

import pytest
from galadril_scribe.official_hipporag import (
    EvidenceLink,
    OfficialHippoRAG,
    OfficialViewCache,
)
from galadril_scribe.retrieval import Fact, Passage


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.openai.com/v1", "max_completion_tokens"),
        ("https://API.OPENAI.COM/v1", "max_completion_tokens"),
        ("https://api.openai.com.attacker.example/v1", "max_tokens"),
        ("https://attacker.example/api.openai.com", "max_tokens"),
        ("https://api.openai.com@attacker.example/v1", "max_tokens"),
        ("https://attacker.example/?host=api.openai.com", "max_tokens"),
    ],
)
def test_output_token_parameter_uses_the_endpoint_hostname(
    base_url: str, expected: str
) -> None:
    from hipporag.llm.gateways import chat_max_tokens_key
    from hipporag.utils.config_utils import BaseConfig

    assert chat_max_tokens_key(BaseConfig(), base_url) == expected


def test_malformed_triples_keep_private_content_out_of_logs(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hipporag.utils.misc_utils import extract_entity_nodes

    logger = logging.getLogger("hipporag.utils.misc_utils")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    extract_entity_nodes([[("private\r\nFORGED RECORD",)]])
    assert len(caplog.records) == 1
    assert "private" not in caplog.text
    assert "FORGED RECORD" not in caplog.text


def test_output_token_parameter_preserves_explicit_override() -> None:
    from hipporag.llm.gateways import chat_max_tokens_key
    from hipporag.utils.config_utils import BaseConfig

    config = BaseConfig(llm_supports_max_completion_tokens=True)
    assert (
        chat_max_tokens_key(config, "https://gateway.example/v1")
        == "max_completion_tokens"
    )


def test_official_sources_match_pinned_checksums() -> None:
    root = Path(__file__).parents[3] / "third_party" / "hipporag" / "upstream"
    provenance = json.loads((root / "PROVENANCE.json").read_bytes())
    assert provenance["commit"] == "d5c8329422e0a0b834a15874545cb6a74b4f9b26"
    assert len(provenance["files"]) >= 60
    for name, digest in provenance["files"].items():
        expected = provenance["patched_files"].get(name, digest)
        assert (
            hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
        )


def corpus() -> tuple[Passage, ...]:
    return (
        Passage(
            id="alice",
            text="Alice works at Lab",
            dense_score=0.9,
            facts=(Fact(subject="Alice", predicate="works at", object="Lab"),),
        ),
        Passage(
            id="lorraine",
            text="Institute is in Lorraine",
            dense_score=0.2,
            facts=(
                Fact(subject="Institute", predicate="is in", object="Lorraine"),
            ),
        ),
        Passage(
            id="decoy",
            text="Malice works at Factory",
            dense_score=0.8,
            facts=(
                Fact(subject="Malice", predicate="works at", object="Factory"),
            ),
        ),
    )


def test_graph_runtime_does_not_pull_in_cuda() -> None:
    import torch

    assert torch.version.cuda is None


def test_ambiguous_fact_serialization_cannot_replace_passage_embedding_input() -> (
    None
):
    with pytest.raises(ValueError, match="serialization"):
        OfficialHippoRAG.embedding_inputs(
            (
                Passage(
                    id="literal",
                    text="('alice', 'owns', 'lab')",
                    facts=(
                        Fact(subject="Alice", predicate="owns", object="Lab"),
                    ),
                ),
            )
        )


def vectors(texts: tuple[str, ...]) -> dict[str, tuple[float, ...]]:
    return {
        text: tuple(
            (byte + 1) / 256
            for byte in hashlib.sha256(text.encode()).digest()[:4]
        )
        for text in texts
    }


def test_warm_view_reuses_index_and_changed_authorized_scope_rebuilds_it() -> (
    None
):
    passages = corpus()
    embeddings = vectors(OfficialHippoRAG.embedding_texts(passages))
    cache = OfficialViewCache()
    try:
        for query in ("Alice", "Where does Alice work?"):
            cache.rank(
                passages,
                embeddings,
                profile="a" * 64,
                links=(),
                query=query,
                query_vector=(1.0, 0.0, 0.0, 0.0),
                recognized=(passages[0].facts[0],),
            )
        assert cache.builds == 1
        result = cache.rank(
            passages[:1],
            embeddings,
            profile="a" * 64,
            links=(),
            query="Alice",
            query_vector=(1.0, 0.0, 0.0, 0.0),
            recognized=(),
        )
        assert cache.builds == 2
        assert {identifier for identifier, _ in result} == {"alice"}
    finally:
        cache.close()


def test_official_index_retrieve_and_delete_use_upstream_package(
    tmp_path: Path,
) -> None:
    passages = corpus()
    texts = OfficialHippoRAG.embedding_texts(passages)
    with OfficialHippoRAG(
        tmp_path, passages, vectors(texts), profile="a" * 64
    ) as engine:
        assert engine.implementation_module == "hipporag.HippoRAG"
        result = engine.retrieve(
            "Who employs Alice?",
            (1.0, 0.0, 0.0, 0.0),
            recognized=(passages[0].facts[0],),
        )
        assert {identifier for identifier, _ in result} == {
            item.id for item in passages
        }
        assert engine.used_graph
        assert all(0 <= score <= 1 for _, score in result)
        engine.delete(("decoy",))
        assert {
            identifier
            for identifier, _ in engine.retrieve(
                "Alice", (1.0, 0.0, 0.0, 0.0), recognized=()
            )
        } == {"alice", "lorraine"}


def test_eskg_extension_preserves_authorized_explicit_links(
    tmp_path: Path,
) -> None:
    passages = corpus()[:2]
    links = (
        EvidenceLink(
            passage_id="alice", entity_id="entity-1", event_id="event-1"
        ),
        EvidenceLink(
            passage_id="lorraine", entity_id="entity-1", event_id="event-2"
        ),
    )
    with OfficialHippoRAG(
        tmp_path,
        passages,
        vectors(OfficialHippoRAG.embedding_texts(passages)),
        profile="a" * 64,
        links=links,
    ) as engine:
        assert engine.identity_link_count == 2
        assert (
            len(
                engine.retrieve(
                    "Alice",
                    (1.0, 0.0, 0.0, 0.0),
                    recognized=(passages[0].facts[0],),
                )
            )
            == 2
        )
    with pytest.raises(ValueError, match="scope"):
        OfficialHippoRAG(
            tmp_path / "forbidden",
            passages,
            vectors(OfficialHippoRAG.embedding_texts(passages)),
            profile="a" * 64,
            links=(
                EvidenceLink(
                    passage_id="forbidden",
                    entity_id="entity-1",
                    event_id="event-1",
                ),
            ),
        )


def test_duplicate_text_retains_distinct_source_ids_and_empty_recognition_is_dense(
    tmp_path: Path,
) -> None:
    passages = (
        Passage(id="one", text="Same evidence"),
        Passage(id="two", text="Same evidence"),
    )
    with OfficialHippoRAG(
        tmp_path,
        passages,
        vectors(OfficialHippoRAG.embedding_texts(passages)),
        profile="a" * 64,
    ) as engine:
        assert {
            identifier
            for identifier, _ in engine.retrieve(
                "evidence", (1.0, 0.0, 0.0, 0.0), recognized=()
            )
        } == {"one", "two"}


def test_missing_or_incompatible_embeddings_fail_before_upstream_index(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="embedding"):
        OfficialHippoRAG(tmp_path, corpus(), {}, profile="a" * 64)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-s"]))
