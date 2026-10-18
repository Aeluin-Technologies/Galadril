"""Cached real model outputs keep retrieval regression tests offline and reproducible."""

from __future__ import annotations

import csv
import hashlib
import math
import re
import statistics
from collections import Counter
from pathlib import Path

from galadril_scribe.retrieval import Fact, Passage, fuse_ranks
from galadril_scribe.retrieval_contracts import (
    EmbeddedTexts,
    EmbedTexts,
    RankPassages,
    Recognition,
)
from galadril_scribe.retrieval_runtime import DocumentRuntime
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    text: str
    facts: tuple[Fact, ...]
    customer_id: int
    source_row: int
    invoice_date: str


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    text: str
    relevant: tuple[str, ...]


class Recognized(BaseModel):
    prompt_sha256: str
    facts: tuple[Fact, ...]


class RecognitionCache(BaseModel):
    model: str
    queries: dict[str, Recognized]


class RerankerCache(BaseModel):
    profile: str
    scores: dict[str, dict[str, FiniteFloat]]
    seconds_per_query: tuple[FiniteFloat, ...]
    peak_rss_bytes: int
    platform: str = "darwin-arm64"


class KnowledgeCache(BaseModel):
    profile: str
    documents: dict[str, tuple[Fact, ...]]
    seconds_per_document: tuple[FiniteFloat, ...]


def knowledge_metrics(
    data: Benchmark, cache: KnowledgeCache
) -> dict[str, float]:
    found = 0
    gold = 0
    for document in data.documents:
        pairs = {
            (fact.subject.casefold(), fact.object.casefold())
            for fact in cache.documents[document.id]
        }
        found += sum(
            (fact.subject.casefold(), fact.object.casefold()) in pairs
            for fact in document.facts
        )
        gold += len(document.facts)
    return {
        "annotated_link_recall": found / gold,
        "documents_with_facts": float(
            sum(bool(facts) for facts in cache.documents.values())
        ),
        "warm_median_seconds": statistics.median(
            cache.seconds_per_document[1:]
        ),
    }


class Benchmark(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int
    source: str
    license: str
    source_sha256: str
    embedding_profile: str
    reranker_profile: str
    documents: tuple[Document, ...] = Field(min_length=16, max_length=32)
    queries: tuple[Query, ...] = Field(min_length=6, max_length=16)
    vectors: dict[str, tuple[FiniteFloat, ...]]
    reranking: dict[str, dict[str, FiniteFloat]]
    recognition: RecognitionCache
    rerankers: dict[str, RerankerCache]
    knowledge: KnowledgeCache | None = None
    knowledge_baseline: KnowledgeCache | None = None

    def vector(self, text: str, query: bool = False) -> tuple[float, ...]:
        key = hashlib.sha256(f"{int(query)}:{text}".encode()).hexdigest()
        return self.vectors[key]

    def recognized(self, query: str) -> tuple[Fact, ...]:
        item = next(item for item in self.queries if item.text == query)
        return self.recognition.queries[item.id].facts


def load_benchmark() -> Benchmark:
    root = Path(__file__).parent / "fixtures"
    data = Benchmark.model_validate_json((root / "retrieval.json").read_bytes())
    if (
        hashlib.sha256((root / "retail.csv").read_bytes()).hexdigest()
        != data.source_sha256
    ):
        raise ValueError("Benchmark no longer matches the ESKG source")
    with (root / "retail.csv").open() as source:
        rows = {int(row["source_row"]): row for row in csv.DictReader(source)}
    identities = {document.id for document in data.documents}
    if len(identities) != len(data.documents):
        raise ValueError("Duplicate benchmark evidence")
    for document in data.documents:
        row = rows[document.source_row]
        allowed = {
            Fact(
                subject=f"Product {row['stock_code']}",
                predicate="appears on",
                object=f"Invoice {row['invoice_no']}",
            ).model_dump_json(),
            Fact(
                subject=f"Invoice {row['invoice_no']}",
                predicate="belongs to",
                object=f"Customer {row['customer_id']}",
            ).model_dump_json(),
            Fact(
                subject=f"Customer {row['customer_id']}",
                predicate="trades with",
                object=row["country"],
            ).model_dump_json(),
        }
        if (
            document.customer_id != int(row["customer_id"])
            or document.invoice_date != row["invoice_date"]
            or any(
                fact.model_dump_json() not in allowed for fact in document.facts
            )
        ):
            raise ValueError(
                "Retrieval facts contradict their ESKG observation"
            )
        data.vector(document.text)
        for fact in document.facts:
            data.vector(f"{fact.subject} {fact.predicate} {fact.object}")
    for vector in data.vectors.values():
        if len(vector) != 1024 or not math.isclose(
            math.fsum(value * value for value in vector), 1.0, abs_tol=1e-5
        ):
            raise ValueError("Invalid cached Qwen embedding")
    for query in data.queries:
        if not query.relevant or not set(query.relevant) <= identities:
            raise ValueError("Invalid benchmark qrels")
        data.vector(query.text, True)
        if set(data.reranking[query.id]) != identities:
            raise ValueError("Incomplete reranking cache")
        known = {
            fact.model_dump_json()
            for document in data.documents
            for fact in document.facts
        }
        if any(
            fact.model_dump_json() not in known
            for fact in data.recognition.queries[query.id].facts
        ):
            raise ValueError("Recognition invented an evidence identity")
    return data


def recall_at(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant or k < 1:
        raise ValueError("Recall requires nonempty qrels and a positive cutoff")
    return len(set(ranked[:k]) & relevant) / len(relevant)


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return max(
        -1.0,
        min(1.0, math.fsum(a * b for a, b in zip(left, right, strict=True))),
    )


def lexical(data: Benchmark, query: str) -> list[str]:
    return bm25_rank(
        {document.id: document.text for document in data.documents}, query
    )


def bm25_rank(documents: dict[str, str], query: str) -> list[str]:
    """Offline reference only; live lifecycle tests execute PostgreSQL BM25."""
    if not documents:
        return []
    tokens = [
        re.findall(r"\w+", text.casefold()) for text in documents.values()
    ]
    terms = set(re.findall(r"\w+", query.casefold()))
    average = max(1.0, sum(map(len, tokens)) / len(tokens))
    frequency = Counter(term for words in tokens for term in set(words))
    ranked = []
    for identifier, words in zip(documents, tokens, strict=True):
        counts = Counter(words)
        score = math.fsum(
            math.log(
                1
                + (len(tokens) - frequency[term] + 0.5)
                / (frequency[term] + 0.5)
            )
            * counts[term]
            * 2.2
            / (counts[term] + 1.2 * (0.25 + 0.75 * len(words) / average))
            for term in terms
            if counts[term]
        )
        if score > 0:
            ranked.append((identifier, score))
    return [
        identifier
        for identifier, score in sorted(
            ranked, key=lambda item: (-item[1], item[0])
        )
    ]


class _PhraseVectors(BaseModel):
    profile_id: str
    vectors: dict[str, tuple[FiniteFloat, ...]]


class CachedEmbedder:
    __slots__ = ("data", "official_vectors")

    def __init__(self, data: Benchmark) -> None:
        self.data = data
        from galadril_scribe.official_hipporag import triple

        phrases = _PhraseVectors.model_validate_json(
            (
                Path(__file__).parent / "fixtures" / "retrieval_phrases.json"
            ).read_bytes()
        )
        if phrases.profile_id != data.embedding_profile:
            raise ValueError("Phrase embedding profile changed")
        self.official_vectors = dict(phrases.vectors)
        for document in data.documents:
            for fact in document.facts:
                self.official_vectors[str(triple(fact))] = data.vector(
                    f"{fact.subject} {fact.predicate} {fact.object}"
                )

    async def embed(self, request: EmbedTexts) -> EmbeddedTexts:
        return EmbeddedTexts(
            profile_id=self.data.embedding_profile,
            vectors=tuple(
                self.official_vectors[text]
                if not request.query and text in self.official_vectors
                else self.data.vector(text, request.query)
                for text in request.texts
            ),
        )


class CachedReranker:
    __slots__ = ("data", "backend")

    def __init__(self, data: Benchmark, backend: str = "bge") -> None:
        self.data = data
        self.backend = backend

    @property
    def profile_id(self) -> str:
        return self.data.rerankers[self.backend].profile

    async def scores(
        self, query: str, documents: tuple[str, ...]
    ) -> tuple[float, ...]:
        item = next(item for item in self.data.queries if item.text == query)
        identifiers = {
            document.text: document.id for document in self.data.documents
        }
        return tuple(
            self.data.rerankers[self.backend].scores[item.id][identifiers[text]]
            for text in documents
        )


async def evaluate(data: Benchmark, backend: str = "bge") -> dict[str, float]:
    totals = {
        mode: 0.0 for mode in ("lexical", "semantic", "hybrid", "hipporag2")
    }
    runtime = DocumentRuntime(
        embedder=CachedEmbedder(data), reranker=CachedReranker(data, backend)
    )
    for query in data.queries:
        query_vector = data.vector(query.text, True)
        dense = sorted(
            (
                (document.id, cosine(query_vector, data.vector(document.text)))
                for document in data.documents
            ),
            key=lambda item: (-item[1], item[0]),
        )
        lexical_ids = lexical(data, query.text)
        dense_ids = [identifier for identifier, score in dense if score >= 0.3]
        fused = fuse_ranks([lexical_ids, dense_ids])
        totals["lexical"] += recall_at(lexical_ids, set(query.relevant), 5)
        totals["semantic"] += recall_at(dense_ids, set(query.relevant), 5)
        documents = {document.id: document for document in data.documents}
        scores = dict(dense)
        passages = tuple(
            Passage(
                id=identifier,
                text=documents[identifier].text,
                dense_score=scores[identifier],
                facts=documents[identifier].facts,
            )
            for identifier, score in fused
        )
        facts = tuple(
            {
                "passage_id": hashlib.sha256(document.id.encode()).hexdigest(),
                "ordinal": ordinal,
                "fact": fact,
                "similarity": cosine(
                    query_vector,
                    data.vector(
                        f"{fact.subject} {fact.predicate} {fact.object}"
                    ),
                ),
            }
            for document in data.documents
            for ordinal, fact in enumerate(document.facts)
        )
        # Runtime contracts use immutable unit hashes; qrels retain source identities.
        source_by_unit = {
            hashlib.sha256(document.id.encode()).hexdigest(): document.id
            for document in data.documents
        }
        hashed = tuple(
            passage.model_copy(
                update={"id": hashlib.sha256(passage.id.encode()).hexdigest()}
            )
            for passage in passages
        )
        recognized = {
            fact.model_dump_json() for fact in data.recognized(query.text)
        }
        indices = tuple(
            index
            for index, item in enumerate(facts)
            if Fact.model_validate(item["fact"]).model_dump_json() in recognized
            and float(item["similarity"]) >= 0.3
        )
        for mode in ("hybrid", "hipporag2"):
            result = await runtime.rank(
                RankPassages(
                    query=query.text,
                    passages=hashed,
                    facts=facts,
                    strategy=mode,
                    recognized=Recognition(indices=indices),
                )
            )
            totals[mode] += recall_at(
                [source_by_unit[item.id] for item in result.passages],
                set(query.relevant),
                5,
            )
    return {
        **{mode: value / len(data.queries) for mode, value in totals.items()},
        "queries": float(len(data.queries)),
    }
