"""Compare full upstream retrieval on the same source-grounded corpus and local models."""

from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

from galadril_scribe.official_hipporag import EvidenceLink, OfficialHippoRAG
from galadril_scribe.retrieval import Fact, Passage, fuse_ranks
from galadril_scribe.retrieval_contracts import EmbedTexts
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
from retrieval_benchmark import (
    Benchmark,
    CachedEmbedder,
    cosine,
    load_benchmark,
    recall_at,
)


class MemoryQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    text: str
    source_row: int
    relevant: tuple[str, ...]
    query_vector: tuple[FiniteFloat, ...]
    reranking: dict[str, FiniteFloat]
    recognized: tuple[Fact, ...]


class MemoryFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_sha256: str
    embedding_profile: str
    reranker_profile: str
    recognition: str
    questions: tuple[MemoryQuestion, ...] = Field(min_length=30, max_length=64)


class MemoryReport(TypedDict):
    queries: int
    source_clusters: int
    eskg_identity_links: int
    identity_origin: str
    recall_at_5: dict[str, float]
    per_question: list[dict[str, object]]


def load_memory() -> tuple[Benchmark, MemoryFixture]:
    data = load_benchmark()
    fixture = MemoryFixture.model_validate_json(
        (
            Path(__file__).parent / "fixtures" / "retrieval_official.json"
        ).read_bytes()
    )
    if (
        fixture.source_sha256 != data.source_sha256
        or fixture.embedding_profile != data.embedding_profile
        or fixture.reranker_profile != data.reranker_profile
    ):
        raise ValueError("Official benchmark provenance changed")
    known = {document.id for document in data.documents}
    facts = {
        fact.model_dump_json()
        for document in data.documents
        for fact in document.facts
    }
    for question in fixture.questions:
        if (
            not question.relevant
            or not set(question.relevant) <= known
            or set(question.reranking) != known
            or len(question.query_vector) != 1024
            or any(
                fact.model_dump_json() not in facts
                for fact in question.recognized
            )
        ):
            raise ValueError("Official benchmark evidence changed")
    return data, fixture


async def official_vectors(
    data: Benchmark, passages: tuple[Passage, ...]
) -> dict[str, tuple[float, ...]]:
    embedder = CachedEmbedder(data)
    inputs = OfficialHippoRAG.embedding_inputs(passages)
    texts = tuple(inputs)
    vectors: dict[str, tuple[float, ...]] = {}
    for start in range(0, len(texts), 32):
        batch = texts[start : start + 32]
        embedded = await embedder.embed(
            EmbedTexts(texts=tuple(inputs[text] for text in batch))
        )
        vectors.update(zip(batch, embedded.vectors, strict=True))
    return vectors


def compare_memory(
    data: Benchmark,
    fixture: MemoryFixture,
    *,
    bindings: Mapping[tuple[int, int], tuple[str, str]] | None = None,
) -> MemoryReport:
    import asyncio

    passages = tuple(
        Passage(id=document.id, text=document.text, facts=document.facts)
        for document in data.documents
    )
    vectors = asyncio.run(official_vectors(data, passages))
    links: list[EvidenceLink] = []
    for document in data.documents:
        key = (document.customer_id, document.source_row)
        if bindings is not None and key not in bindings:
            raise ValueError("Missing live ESKG binding")
        entity, event = (
            bindings[key]
            if bindings is not None
            else (
                f"customer:{document.customer_id}",
                f"source-row:{document.source_row}",
            )
        )
        links.append(EvidenceLink(document.id, entity, event))
    methods = (
        "dense",
        "official",
        "official_eskg",
        "official_reranked",
        "official_eskg_reranked",
    )
    totals = dict.fromkeys(methods, 0.0)
    rows: list[dict[str, object]] = []
    with TemporaryDirectory(prefix="official-memory-benchmark-") as directory:
        with (
            OfficialHippoRAG(
                Path(directory) / "plain",
                passages,
                vectors,
                profile=data.embedding_profile,
            ) as plain,
            OfficialHippoRAG(
                Path(directory) / "eskg",
                passages,
                vectors,
                profile=data.embedding_profile,
                links=tuple(links),
            ) as enriched,
        ):
            for question in fixture.questions:
                dense = sorted(
                    passages,
                    key=lambda item: (
                        -cosine(question.query_vector, data.vector(item.text)),
                        item.id,
                    ),
                )
                ranked = {
                    "dense": [item.id for item in dense],
                    "official": [
                        identifier
                        for identifier, _ in plain.retrieve(
                            question.text,
                            question.query_vector,
                            recognized=question.recognized,
                        )
                    ],
                    "official_eskg": [
                        identifier
                        for identifier, _ in enriched.retrieve(
                            question.text,
                            question.query_vector,
                            recognized=question.recognized,
                        )
                    ],
                }
                for method in ("official", "official_eskg"):
                    order = {
                        identifier: index
                        for index, identifier in enumerate(ranked[method][:32])
                    }
                    ranked[method + "_reranked"] = sorted(
                        order,
                        key=lambda identifier: (
                            -question.reranking[identifier],
                            order[identifier],
                        ),
                    )
                    fusion = dict(
                        fuse_ranks(
                            [ranked[method + "_reranked"], ranked[method][:32]]
                        )
                    )
                    ranked[method + "_reranked"].sort(
                        key=lambda identifier: (
                            -fusion[identifier],
                            -question.reranking[identifier],
                            identifier,
                        )
                    )
                recalls = {
                    method: recall_at(ranked[method], set(question.relevant), 5)
                    for method in methods
                }
                for method, score in recalls.items():
                    totals[method] += score
                rows.append(
                    {
                        "id": question.id,
                        "source_row": question.source_row,
                        "recall_at_5": recalls,
                        "ranked": {
                            method: ranked[method][:5] for method in methods
                        },
                    }
                )
    return MemoryReport(
        queries=len(fixture.questions),
        source_clusters=len(
            {question.source_row for question in fixture.questions}
        ),
        eskg_identity_links=len(links),
        identity_origin="live_eskg"
        if bindings is not None
        else "source_fixture",
        recall_at_5={
            method: score / len(fixture.questions)
            for method, score in totals.items()
        },
        per_question=rows,
    )
