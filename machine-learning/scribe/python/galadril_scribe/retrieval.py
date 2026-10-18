"""Bounded document projections never promote extracted relationships into ESKG facts."""

import math
import unicodedata
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, StrictFloat


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    object: str = Field(min_length=1, max_length=256)


class Passage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=16384)
    dense_score: StrictFloat = Field(
        default=0.0, ge=-1, le=1, allow_inf_nan=False
    )
    facts: tuple[Fact, ...] = Field(default=(), max_length=8)


@dataclass(frozen=True, slots=True)
class Chunk:
    text: str
    start: int
    end: int


def structural_chunks(
    text: str, *, max_characters: int = 2048, overlap: int = 256
) -> list[Chunk]:
    """Preserve source spans while preferring paragraph and line boundaries."""
    if (
        len(text.encode()) > 1048576
        or not 128 <= max_characters <= 8192
        or not 0 <= overlap < max_characters // 2
    ):
        raise ValueError("Document chunking budget exceeded")
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + max_characters, len(text))
        if end < len(text):
            floor = start + max_characters // 2
            for separator in ("\n\n", "\n", ". ", " "):
                boundary = text.rfind(separator, floor, end)
                if boundary >= floor:
                    end = boundary + len(separator)
                    break
        part = text[start:end]
        if part.strip():
            chunks.append(Chunk(part, start, end))
        if len(chunks) > 2048:
            raise ValueError("Document chunk count exceeded")
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def fuse_ranks(
    branches: list[list[str]], *, constant: int = 60
) -> list[tuple[str, float]]:
    """Rank fusion avoids interpreting BM25 and cosine as calibrated probabilities."""
    scores: dict[str, float] = {}
    if not 1 <= constant <= 256 or len(branches) > 4:
        raise ValueError("Fusion budget exceeded")
    for branch in branches:
        if len(branch) > 256:
            raise ValueError("Candidate budget exceeded")
        seen: set[str] = set()
        for rank, identifier in enumerate(branch, 1):
            if identifier not in seen:
                scores[identifier] = scores.get(identifier, 0.0) + 1 / (
                    constant + rank
                )
                seen.add(identifier)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def hipporag_rank(
    passages: list[Passage],
    *,
    recognized: list[tuple[str, int, float]],
    damping: float = 0.5,
) -> list[tuple[str, float]]:
    """Rank a bounded associative projection inspired by HippoRAG 2.

    Callers must supply only currently authorized passages and recognized fact indices.
    Passage/entity and relation edges remain a disposable retrieval projection.
    The upstream graph kernel is evaluated separately; this is not an exact reproduction.
    """
    if len(passages) > 256 or len(recognized) > 8 or not 0 < damping < 1:
        raise ValueError("Graph retrieval budget exceeded")
    if not passages:
        return []
    identifiers: dict[str, int] = {}
    nodes: dict[str, int] = {}
    edges: list[dict[int, float]] = []

    def node(key: str) -> int:
        index = nodes.get(key)
        if index is None:
            if len(nodes) >= 8192:
                raise ValueError("Graph working set exceeded")
            index = len(nodes)
            nodes[key] = index
            edges.append({})
        return index

    def phrase(value: str) -> int:
        return node(
            "e:"
            + " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        )

    def connect(left: int, right: int) -> None:
        if left != right:
            edges[left][right] = edges[left].get(right, 0.0) + 1.0
            edges[right][left] = edges[right].get(left, 0.0) + 1.0

    facts: dict[tuple[str, int], tuple[int, int]] = {}
    for passage in passages:
        if passage.id in identifiers:
            raise ValueError("Duplicate evidence identifier")
        index = node("p:" + passage.id)
        identifiers[passage.id] = index
        for ordinal, fact in enumerate(passage.facts):
            subject, target = phrase(fact.subject), phrase(fact.object)
            connect(index, subject)
            connect(index, target)
            connect(subject, target)
            facts[(passage.id, ordinal)] = (subject, target)
    restart = [0.0] * len(nodes)
    for identifier, ordinal, score in recognized:
        pair = facts.get((identifier, ordinal))
        if pair is not None and math.isfinite(score) and score > 0:
            for index in pair:
                restart[index] += min(score, 1.0) / 2
    recognized_mass = sum(restart)
    has_recognition = recognized_mass > 0
    if has_recognition:
        restart = [0.95 * value / recognized_mass for value in restart]
    dense_mass = sum(max(0.0, passage.dense_score) for passage in passages)
    for passage in passages:
        if dense_mass > 0:
            restart[identifiers[passage.id]] += (
                max(0.0, passage.dense_score)
                / dense_mass
                * (0.05 if has_recognition else 1.0)
            )
    mass = sum(restart)
    if mass == 0:
        for index in identifiers.values():
            restart[index] = 1.0 / len(passages)
    else:
        restart = [value / mass for value in restart]
    adjacency = [
        tuple(
            (index, weight / sum(neighbours.values()))
            for index, weight in neighbours.items()
        )
        for neighbours in edges
    ]
    ranks = restart.copy()
    for _ in range(128):
        dangling = sum(
            ranks[index]
            for index, neighbours in enumerate(adjacency)
            if not neighbours
        )
        next_ranks = [
            (1 - damping + damping * dangling) * value for value in restart
        ]
        for origin, neighbours in enumerate(adjacency):
            contribution = damping * ranks[origin]
            for destination, weight in neighbours:
                next_ranks[destination] += contribution * weight
        difference = sum(
            abs(left - right)
            for left, right in zip(ranks, next_ranks, strict=True)
        )
        ranks = next_ranks
        if difference < 1e-9:
            break
    passage_mass = sum(ranks[index] for index in identifiers.values())
    return sorted(
        (
            (identifier, ranks[index] / passage_mass if passage_mass else 0.0)
            for identifier, index in identifiers.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )
