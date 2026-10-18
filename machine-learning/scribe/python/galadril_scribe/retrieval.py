"""Bounded document projections never promote extracted relationships into ESKG facts."""

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
