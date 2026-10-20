"""Private document operations carry evidence, never database or object-store authority."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictInt,
    model_validator,
)

from galadril_scribe.retrieval import Fact, Passage

UnitId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Vector = Annotated[
    tuple[FiniteFloat, ...], Field(min_length=1, max_length=4096)
]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )


class TextUnit(Contract):
    ordinal: StrictInt = Field(ge=0, le=2047)
    text: str = Field(min_length=1, max_length=16384)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=1)
    page: StrictInt | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def source_span(self) -> "TextUnit":
        if self.end - self.start != len(self.text):
            raise ValueError("Unit must preserve its exact source span")
        return self


class ParseDocument(Contract):
    mime: Literal[
        "text/plain",
        "text/csv",
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
    ]
    content: str = Field(min_length=1, max_length=34952536)
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")


class ParsedDocument(Contract):
    parser_revision: str = Field(default="structural-v1", max_length=128)
    units: tuple[TextUnit, ...] = Field(max_length=2048)
    warnings: tuple[str, ...] = Field(default=(), max_length=8)


class EmbedTexts(Contract):
    texts: tuple[Annotated[str, Field(min_length=1, max_length=16384)], ...] = (
        Field(min_length=1, max_length=32)
    )
    query: bool = False


class EmbeddedTexts(Contract):
    profile_id: UnitId
    vectors: tuple[Vector, ...] = Field(min_length=1, max_length=32)


class ExtractPassage(Contract):
    id: UnitId
    text: str = Field(min_length=1, max_length=16384)


class ExtractFacts(Contract):
    passages: tuple[ExtractPassage, ...] = Field(min_length=1, max_length=8)


class PassageFacts(Contract):
    id: UnitId
    facts: tuple[Fact, ...] = Field(default=(), max_length=8)


class ExtractedFacts(Contract):
    passages: tuple[PassageFacts, ...] = Field(max_length=8)
    warnings: tuple[str, ...] = Field(default=(), max_length=8)


class FactCandidate(Contract):
    passage_id: UnitId
    ordinal: StrictInt = Field(ge=0, le=7)
    fact: Fact
    similarity: FiniteFloat = Field(ge=-1, le=1)


class Recognition(Contract):
    indices: tuple[Annotated[StrictInt, Field(ge=0, le=63)], ...] = Field(
        default=(), max_length=5
    )


class RecognizeFacts(Contract):
    query: str = Field(min_length=1, max_length=2048)
    facts: tuple[FactCandidate, ...] = Field(max_length=64)


class RecognizedFacts(Recognition):
    warnings: tuple[str, ...] = Field(default=(), max_length=8)


class RankPassages(Contract):
    query: str = Field(min_length=1, max_length=2048)
    passages: tuple[Passage, ...] = Field(min_length=1, max_length=256)
    facts: tuple[FactCandidate, ...] = Field(default=(), max_length=64)
    strategy: Literal["hybrid", "hipporag2"] = "hybrid"
    min_relevance: FiniteFloat = Field(default=0.0, ge=0, le=1)
    recognized: Recognition | None = None
    eskg_links: tuple["GraphLink", ...] = Field(default=(), max_length=1024)

    @model_validator(mode="after")
    def graph_scope(self) -> "RankPassages":
        identifiers = {passage.id for passage in self.passages}
        if len(identifiers) != len(self.passages) or any(
            link.passage_id not in identifiers for link in self.eskg_links
        ):
            raise ValueError("Graph links must stay inside the evidence scope")
        return self


class GraphLink(Contract):
    passage_id: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    event_id: str = Field(min_length=1, max_length=256)


class RankedPassage(Contract):
    id: str = Field(min_length=1, max_length=128)
    score: FiniteFloat = Field(ge=0, le=1)
    graph_score: FiniteFloat | None = Field(default=None, ge=0, le=1)


class RankedPassages(Contract):
    passages: tuple[RankedPassage, ...] = Field(max_length=64)
    warnings: tuple[str, ...] = Field(default=(), max_length=8)
    reranker_profile: str | None = Field(default=None, max_length=128)
    engine: Literal["hybrid", "hipporag2-official", "dense-fallback"] = "hybrid"
