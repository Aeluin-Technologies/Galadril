"""Shared types for galadril-vision."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, unique
from typing import Any
from uuid import uuid4


def _generate_id() -> str:
    return uuid4().hex


@unique
class ProcessingStatus(StrEnum):
    """Status of record processing in the pipeline."""

    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


@unique
class EntityType(StrEnum):
    """Types of entities that can be extracted and linked in the graph."""

    PERSON = "Person"
    ORGANIZATION = "Organization"
    LOCATION = "Location"
    ACCOUNT = "Account"  # Financial account.
    DOCUMENT = "Document"
    VEHICLE = "Vehicle"
    BUILD = "Building"
    CONCEPT = "Concept"
    WEAPON = "Weapon"
    UNKNOWN = "Unknown"


@unique
class EventType(StrEnum):
    """Types of events (E) in the ESKG."""

    OBSERVATION = "Observation"
    TRANSACTION = "Transaction"
    COMMUNICATION = "Communication"
    DOCUMENT_PUBLISHED = "DocumentPublished"

    @classmethod
    def from_str(cls, value: Any) -> "EventType":
        try:
            return cls(value)
        except (ValueError, TypeError):
            return cls.OBSERVATION


@unique
class EmbeddingModality(StrEnum):
    """Supported modalities for pgvectorscale."""

    FACE = "face"
    VOICE = "voice"
    IMAGE = "image"
    TEXT = "text"


@dataclass(slots=True)
class EntityEmbedding:
    """A generic embedding record for the unified vector store."""

    embedding_id: str = field(default_factory=_generate_id)
    entity_id: str | None = None
    modality: EmbeddingModality = EmbeddingModality.FACE
    vector: list[float] = field(default_factory=list)
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    is_unknown: bool = True


@dataclass(slots=True)
class EventRecord:
    """An Event (E) node in the ESKG."""

    event_id: str = field(default_factory=_generate_id)
    event_type: EventType = EventType.OBSERVATION
    timestamp: datetime = field(default_factory=datetime.now)
    location_coords: list[float] | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EntityStateRecord:
    """A State (S) record stored in TimescaleDB."""

    entity_id: str
    event_id: str
    state_type: str
    state_value: dict[str, Any]
    event_time: datetime


@dataclass(frozen=True, slots=True)
class GraphVertex:
    """A vertex to create/update in Apache AGE."""

    vertex_id: str
    label: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """An edge to create in Apache AGE between two entities."""

    source_vertex_id: str
    target_vertex_id: str
    edge_type: str
    properties: dict[str, Any] = field(default_factory=dict)
