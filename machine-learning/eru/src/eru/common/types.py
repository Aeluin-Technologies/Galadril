"""Core types and protocols used throughout Eru."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar
from pydantic import BaseModel, Field

TGraph = TypeVar("TGraph", bound=BaseModel)


class EntityMention(BaseModel):
    """Represents a single structural occurrence of an entity in raw text."""

    text: str
    start_char: int
    end_char: int
    score: float


class ExtractedCandidate(BaseModel):
    """An initial raw entity candidate captured during extraction."""

    text: str
    labels: list[str]
    mentions: list[EntityMention] = Field(default_factory=list)
    confidence: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReferenceCluster(BaseModel):
    """A grouping of text expressions that refer back to the same entity."""

    canonical_mention: str
    mentions: list[str]


class ReferenceResolution(BaseModel):
    """Wrapper containing identified reference clusters."""

    clusters: list[ReferenceCluster]


class SemanticEntity(BaseModel):
    """Normalized structural representation of an entity mapping."""

    canonical_name: str
    canonical_label: str
    aliases: list[str]


class SemanticNormalization(BaseModel):
    """Wrapper containing semantically normalized entities."""

    entities: list[SemanticEntity]


class CanonicalEntity(BaseModel):
    """The unified, deduplicated entity definition used inside the pipeline."""

    canonical_name: str
    labels: list[str]
    aliases: list[str] = Field(default_factory=list)
    mentions: list[EntityMention] = Field(default_factory=list)
    confidence: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImplicitEntity(BaseModel):
    """An unstated entity derived or logically inferred from context context."""

    canonical_name: str
    canonical_label: str
    evidence: str


class ImplicitEntityResult(BaseModel):
    """Wrapper containing inferred implicit entities."""

    entities: list[ImplicitEntity]


class RelationCandidate(BaseModel):
    """A structural proposal of a directional connection between two entities."""

    source_id: str
    target_id: str


class RelationDef(BaseModel):
    """The schema structure definition for a graph relation type."""

    name: str
    description: str
    examples: list[str] = Field(default_factory=list)


class CandidateExtractor(Protocol):
    """Protocol for extracting raw entity candidates from text."""

    def extract(self, text: str) -> list[ExtractedCandidate]:
        """Extracts initial candidate entities from raw string inputs."""
        ...


class CoreferenceResolver(Protocol):
    """Protocol for resolving entity mentions and text aliases."""

    def resolve(
        self,
        text: str,
        candidates: list[ExtractedCandidate],
    ) -> ReferenceResolution:
        """Clusters related raw textual mentions to a root representation."""
        ...


class SemanticNormalizer(Protocol):
    """Protocol for standardizing entity naming conventions and typings."""

    def normalize(
        self,
        text: str,
        candidates: list[ExtractedCandidate],
        references: ReferenceResolution,
    ) -> SemanticNormalization:
        """Aligns extracted mentions to clean, canonical naming structures."""
        ...


class ImplicitEntityGenerator(Protocol):
    """Protocol for identifying unstated context clues as actual entities."""

    def generate(
        self,
        text: str,
        entities: list[CanonicalEntity],
    ) -> ImplicitEntityResult:
        """Infers hidden entities based on the text context and visible graph."""
        ...


class RelationCandidateGenerator(Protocol):
    """Protocol for building directional relation candidate pairs."""

    def propose(
        self, entities: list[CanonicalEntity]
    ) -> list[RelationCandidate]:
        """Generates possible connection pairs between existing entities."""
        ...


class SemanticReasoner(Protocol):
    """Protocol for reasoning over candidates to create a valid graph schema."""

    def reason(
        self,
        text: str,
        entities: list[CanonicalEntity],
        candidates: list[RelationCandidate],
        schema: Any,
    ) -> TGraph:
        """Evaluates textual intent and proposed edges to build the final graph."""
        ...


class LogicValidator(Protocol):
    """Protocol for verifying final graph output against business rules."""

    def validate(self, graph: TGraph) -> TGraph:
        """Validates graph structure constraints, returning the verified graph."""
        ...
