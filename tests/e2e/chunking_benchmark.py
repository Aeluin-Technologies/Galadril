"""Cached native embeddings compare boundaries and experimental causal pooling."""

import hashlib
import math
import statistics
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
from retrieval_benchmark import cosine, load_benchmark


class CachedChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    vector: tuple[FiniteFloat, ...]


class ChunkingVariant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile: str
    chunks: tuple[CachedChunk, ...]
    query_vectors: dict[str, tuple[FiniteFloat, ...]]


class ChunkingCache(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_sha256: str
    text: str
    evidence_spans: dict[str, tuple[int, int]]
    relevant: dict[str, tuple[str, ...]]
    variants: dict[str, ChunkingVariant]


class ChunkingReport(TypedDict):
    queries: int
    late_chunking_status: str
    source_evidence_recall_at_5: dict[str, float]
    chunk_counts: dict[str, int]


def load_chunking() -> ChunkingCache:
    """Reject stale inputs, changed qrels, or incompatible cached model outputs."""
    root = Path(__file__).parent / "fixtures"
    cache = ChunkingCache.model_validate_json(
        (root / "retrieval_chunking.json").read_bytes()
    )
    source = load_benchmark()
    if (
        cache.source_sha256
        != hashlib.sha256((root / "retail.csv").read_bytes()).hexdigest()
    ):
        raise ValueError("Chunking cache does not match the ESKG source")
    if cache.relevant != {query.id: query.relevant for query in source.queries}:
        raise ValueError("Chunking qrels differ from source evidence")
    for document in source.documents:
        start, end = cache.evidence_spans[document.id]
        if cache.text[start:end] != document.text:
            raise ValueError("Chunking evidence location changed")
    for variant in cache.variants.values():
        if len(variant.chunks) < 6:
            raise ValueError("Recall@5 needs more than five chunk candidates")
        for chunk in variant.chunks:
            if (
                not 0 <= chunk.start < chunk.end <= len(cache.text)
                or chunk.text != cache.text[chunk.start : chunk.end]
            ):
                raise ValueError("Chunk source span changed")
        for vector in [
            *(chunk.vector for chunk in variant.chunks),
            *variant.query_vectors.values(),
        ]:
            if len(vector) != 1024 or not math.isclose(
                math.fsum(value * value for value in vector), 1, abs_tol=1e-5
            ):
                raise ValueError("Invalid cached chunk embedding")
    return cache


def chunking_report(data: ChunkingCache) -> ChunkingReport:
    """Count a source passage once when a top-five chunk covers at least half of it."""
    scores = {}
    for label, variant in data.variants.items():
        values = []
        for query, relevant in data.relevant.items():
            ranked = sorted(
                variant.chunks,
                key=lambda chunk: (
                    -cosine(variant.query_vectors[query], chunk.vector),
                    chunk.start,
                ),
            )[:5]
            found = 0
            for identifier in relevant:
                start, end = data.evidence_spans[identifier]
                if any(
                    max(0, min(end, chunk.end) - max(start, chunk.start))
                    >= (end - start) / 2
                    for chunk in ranked
                ):
                    found += 1
            values.append(found / len(relevant))
        scores[label] = statistics.mean(values)
    return {
        "queries": len(data.relevant),
        "late_chunking_status": "experimental_causal_pooling_only",
        "source_evidence_recall_at_5": scores,
        "chunk_counts": {
            label: len(variant.chunks)
            for label, variant in data.variants.items()
        },
    }
