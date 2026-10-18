"""Document ranking preserves evidence IDs and only traverses the supplied scope."""

import base64
import hashlib
import sys

import pytest
from eru.documents import Projection
from galadril_scribe.retrieval import (
    Fact,
    Passage,
    fuse_ranks,
    hipporag_rank,
    structural_chunks,
)
from galadril_scribe.retrieval_contracts import (
    ParseDocument,
    RankPassages,
    RecognizedFacts,
    RecognizeFacts,
)
from galadril_scribe.retrieval_runtime import DocumentRuntime
from pydantic_ai.models.test import TestModel


def test_recognition_anchors_distinctive_names_without_matching_substrings() -> (
    None
):
    from galadril_scribe.retrieval_contracts import RecognizeFacts
    from galadril_scribe.retrieval_eru import anchored_indices

    request = RecognizeFacts(
        query="Where does Alice work?",
        facts=(
            {
                "passage_id": "a" * 64,
                "ordinal": 0,
                "fact": {
                    "subject": "Alice",
                    "predicate": "works at",
                    "object": "Laboratory",
                },
                "similarity": 0.8,
            },
            {
                "passage_id": "b" * 64,
                "ordinal": 0,
                "fact": {
                    "subject": "Malice",
                    "predicate": "works at",
                    "object": "Laboratory",
                },
                "similarity": 0.8,
            },
        ),
    )
    assert anchored_indices(request) == (0,)


def test_dense_restart_budget_does_not_dilute_recognized_graph_seeds_as_candidates_grow() -> (
    None
):
    passages = [
        Passage(
            id="start",
            text="Alice owns Lab",
            dense_score=0.8,
            facts=(Fact(subject="Alice", predicate="owns", object="Lab"),),
        ),
        Passage(
            id="bridge",
            text="Lab belongs to Institute",
            dense_score=0.2,
            facts=(
                Fact(subject="Lab", predicate="belongs to", object="Institute"),
            ),
        ),
        Passage(
            id="answer",
            text="Institute is in Lorraine",
            dense_score=0.1,
            facts=(
                Fact(subject="Institute", predicate="is in", object="Lorraine"),
            ),
        ),
    ]
    passages.extend(
        Passage(
            id=f"decoy-{index}",
            text="Generic unrelated passage",
            dense_score=0.9,
        )
        for index in range(200)
    )
    ranked = hipporag_rank(passages, recognized=[("start", 0, 0.8)])
    assert {"start", "bridge", "answer"} <= {
        identifier for identifier, _ in ranked[:5]
    }


@pytest.mark.anyio
async def test_local_extraction_precedes_chat_and_rejected_relations_are_visible() -> (
    None
):
    from eru.documents import Fact as EruFact
    from eru.documents import Projection
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage

    class Knowledge:
        async def extract(self, text: str) -> Projection:
            return Projection(
                facts=(
                    EruFact(
                        subject="Alice", predicate="works in", object="Lorraine"
                    ),
                ),
                candidates=2,
                rejected=1,
            )

        async def recognize(self, request: RecognizeFacts) -> RecognizedFacts:
            raise AssertionError("Recognition was not requested")

    runtime = DocumentRuntime(knowledge=Knowledge())
    result = await runtime.extract(
        ExtractFacts(
            passages=(
                ExtractPassage(id="a" * 64, text="Alice works in Lorraine"),
            )
        )
    )
    assert result.passages[0].facts[0].subject == "Alice"
    assert "graph_relations_rejected" in result.warnings


@pytest.mark.anyio
async def test_local_extraction_failure_returns_partial_evidence_without_failing_conversation() -> (
    None
):
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage

    class Knowledge:
        async def extract(self, text: str) -> Projection:
            raise RuntimeError("private-document")

        async def recognize(self, request: RecognizeFacts) -> RecognizedFacts:
            raise RuntimeError("private-document")

    result = await DocumentRuntime(knowledge=Knowledge()).extract(
        ExtractFacts(
            passages=(
                ExtractPassage(id="a" * 64, text="Alice works in Lorraine"),
            )
        )
    )
    assert result.passages[0].facts == ()
    assert result.warnings == ("graph_extraction_unavailable",)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_optional_chat_escalation_still_rejects_invented_evidence() -> (
    None
):
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage

    runtime = DocumentRuntime(
        recognition_model=TestModel(
            custom_output_args={
                "facts": [
                    {
                        "subject": "Invented",
                        "predicate": "owns",
                        "object": "Lorraine",
                        "evidence": "Alice works in Lorraine",
                    }
                ]
            }
        )
    )
    result = await runtime.extract(
        ExtractFacts(
            passages=(
                ExtractPassage(id="a" * 64, text="Alice works in Lorraine"),
            )
        )
    )
    assert not result.passages[0].facts
    assert "graph_relations_rejected" in result.warnings


@pytest.mark.anyio
async def test_document_parsing_preserves_provenance_and_verifies_the_source_checksum() -> (
    None
):
    content = "La charte prévoit une procédure en cas d’irrégularité.".encode()
    runtime = DocumentRuntime()
    parsed = await runtime.parse(
        ParseDocument(
            mime="text/plain",
            content=base64.b64encode(content).decode(),
            checksum=hashlib.sha256(content).hexdigest(),
        )
    )
    assert parsed.units[0].text == content.decode()
    assert parsed.units[0].start == 0
    with pytest.raises(ValueError):
        await runtime.parse(
            ParseDocument(
                mime="text/plain",
                content=base64.b64encode(content).decode(),
                checksum="a" * 64,
            )
        )


@pytest.mark.anyio
async def test_hipporag_recognition_and_local_reranking_keep_source_identifiers() -> (
    None
):
    class Reranker:
        profile_id = "local-reranker"

        async def scores(
            self, query: str, documents: tuple[str, ...]
        ) -> tuple[float, ...]:
            return tuple(0.8 for _ in documents)

    first, second = "a" * 64, "b" * 64
    passages = (
        Passage(
            id=first,
            text="Alice directs the laboratory.",
            dense_score=0.9,
            facts=(
                Fact(subject="Alice", predicate="directs", object="Laboratory"),
            ),
        ),
        Passage(
            id=second,
            text="The laboratory belongs to Lorraine.",
            dense_score=0.1,
            facts=(
                Fact(
                    subject="Laboratory",
                    predicate="belongs to",
                    object="Lorraine",
                ),
            ),
        ),
    )
    runtime = DocumentRuntime(
        reranker=Reranker(),
        recognition_model=TestModel(custom_output_args={"indices": [0]}),
    )
    result = await runtime.rank(
        RankPassages(
            query="Where is Alice’s laboratory?",
            passages=passages,
            strategy="hipporag2",
            facts=(
                {
                    "passage_id": first,
                    "ordinal": 0,
                    "fact": passages[0].facts[0],
                    "similarity": 0.9,
                },
            ),
        )
    )
    assert {item.id for item in result.passages} == {first, second}
    assert all(item.graph_score is not None for item in result.passages)
    assert result.reranker_profile == "local-reranker"


@pytest.mark.anyio
async def test_reranker_failure_degrades_without_losing_the_conversation() -> (
    None
):
    class Reranker:
        profile_id = "unavailable"

        async def scores(
            self, query: str, documents: tuple[str, ...]
        ) -> tuple[float, ...]:
            raise RuntimeError("private backend diagnostics")

    runtime = DocumentRuntime(reranker=Reranker())
    selected = RankPassages(
        query="Proof",
        passages=(Passage(id="proof", text="Evidence", dense_score=0.9),),
    )
    result = await runtime.rank(selected)
    assert result.passages[0].id == "proof"
    assert "reranker_unavailable" in result.warnings
    assert "private backend" not in result.model_dump_json()


@pytest.mark.anyio
async def test_pdf_parser_preserves_page_locations_and_rejects_image_only_documents() -> (
    None
):
    from galadril_scribe.retrieval_models import PdfParser

    parser = PdfParser()
    with pytest.raises(ValueError):
        await parser.parse(b"not a PDF", "application/pdf")
    with pytest.raises(ValueError):
        await parser.parse(b"image", "image/png")


@pytest.mark.anyio
async def test_private_document_api_has_bounded_inputs_and_no_storage_authority() -> (
    None
):
    import httpx
    from galadril_scribe.contracts import Settings
    from galadril_scribe.runtime import Runtime, create_app

    async with httpx.AsyncClient() as transport:
        runtime = Runtime(
            Settings(gateway_tools_url="http://gateway/internal/chat/tools"),
            transport,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            content = b"public evidence"
            response = await client.post(
                "/documents/parse",
                json={
                    "mime": "text/plain",
                    "content": base64.b64encode(content).decode(),
                    "checksum": hashlib.sha256(content).hexdigest(),
                },
            )
            assert response.status_code == 200
            assert response.json()["units"][0]["text"] == content.decode()
            response = await client.post(
                "/documents/parse",
                json={"url": "http://storage/private", "tenant": "other"},
            )
            assert response.status_code == 422


def test_structural_chunks_preserve_exact_locations_and_reject_unbounded_input() -> (
    None
):
    text = (
        "Préambule\n\n"
        + "university evidence " * 50
        + "\n\nArticle 2\nAppeals require review."
    )
    chunks = structural_chunks(text, max_characters=256, overlap=32)
    assert len(chunks) > 2
    assert all(text[chunk.start : chunk.end] == chunk.text for chunk in chunks)
    assert all(len(chunk.text) <= 256 for chunk in chunks)
    assert chunks[-1].end == len(text)
    with pytest.raises(ValueError):
        structural_chunks("a" * 1048577)


def test_rank_fusion_is_deterministic_without_comparing_incompatible_scores() -> (
    None
):
    assert fuse_ranks([["a", "b"], ["b", "c"]])[0][0] == "b"
    assert len(fuse_ranks([["a", "a"], ["b"]])) == 2


def test_hipporag_two_hop_evidence_reaches_an_indirect_passage() -> None:
    passages = [
        Passage(
            id="first",
            text="Alice directs the laboratory.",
            dense_score=0.9,
            facts=(
                Fact(subject="Alice", predicate="directs", object="Laboratory"),
            ),
        ),
        Passage(
            id="second",
            text="The laboratory belongs to Lorraine.",
            dense_score=0.1,
            facts=(
                Fact(
                    subject="Laboratory",
                    predicate="belongs to",
                    object="Lorraine",
                ),
            ),
        ),
        Passage(
            id="unrelated",
            text="The bank is in Paris.",
            dense_score=0.2,
            facts=(Fact(subject="Bank", predicate="in", object="Paris"),),
        ),
    ]
    ranked = hipporag_rank(passages, recognized=[("first", 0, 0.95)])
    assert [item[0] for item in ranked[:2]] == ["first", "second"]
    assert all(item[1] >= 0 for item in ranked)
    assert {identifier for identifier, score in ranked} == {
        "first",
        "second",
        "unrelated",
    }
    assert hipporag_rank(passages[:1], recognized=[("second", 0, 1.0)]) == [
        ("first", 1.0)
    ]


def test_empty_recognition_uses_dense_passage_evidence_and_does_not_invent_edges() -> (
    None
):
    passages = [
        Passage(id="a", text="One", dense_score=0.1),
        Passage(id="b", text="Two", dense_score=0.9),
    ]
    assert hipporag_rank(passages, recognized=[])[0][0] == "b"
    assert hipporag_rank([], recognized=[]) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
