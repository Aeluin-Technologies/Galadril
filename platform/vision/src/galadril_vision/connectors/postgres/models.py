from datetime import datetime
from typing import Any, Dict, List

from geoalchemy2 import Geometry
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models in this application."""

    pass


class EntityState(Base):
    """Represents the state of an entity at a specific point in time."""

    __tablename__ = "entity_states"

    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
    entity_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String)
    state_type: Mapped[str] = mapped_column(String)
    state_value: Mapped[Dict[str, Any]] = mapped_column(JSONB)
    geom: Mapped[Any] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
    )

    __table_args__ = (
        Index(
            "idx_entity_states_tenant_entity_time",
            tenant_id,
            entity_id,
            event_time.desc(),
        ),
        Index(
            "idx_entity_states_geom",
            geom,
            postgresql_using="gist",
        ),
        Index(
            "idx_entity_states_name_trgm",
            text("(state_value->>'name') gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )


class EskgEvent(Base):
    """Represents an event recorded within the event-driven knowledge graph."""

    __tablename__ = "eskg_events"

    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
    )
    properties: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        server_default=text("'{}'::jsonb"),
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
    )

    __table_args__ = (
        Index(
            "idx_eskg_events_tenant_type_time",
            tenant_id,
            event_type,
            event_time.desc(),
        ),
    )


class CausalRun(Base):
    """Tracks the execution and results of causal analysis runs."""

    __tablename__ = "causal_runs"

    cache_key: Mapped[str] = mapped_column(
        String,
        primary_key=True,
    )
    target: Mapped[str] = mapped_column(String)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
    )
    status: Mapped[str] = mapped_column(String)
    result_summary: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index(
            "idx_causal_runs_window",
            window_start.desc(),
            window_end.desc(),
        ),
        Index(
            "idx_causal_runs_target",
            target,
            created_at.desc(),
        ),
    )


class AuthzOutbox(Base):
    """Outbox pattern implementation for propagating authorization tuples.

    Acts as a persistent transactional queue storing access control updates
    that need to be synchronized with remote authorization services reliably.
    """

    __tablename__ = "authz_outbox"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    tenant_id: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    tuples_json: Mapped[List[Any]] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(
        Integer,
        server_default=text("0"),
    )
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
    )

    __table_args__ = (
        CheckConstraint(
            "tenant_id <> ''",
            name="ck_authz_outbox_tenant_nonempty",
        ),
        CheckConstraint(
            "object_id <> ''",
            name="ck_authz_outbox_object_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(tuples_json) = 'array'",
            name="ck_authz_outbox_tuples_array",
        ),
        Index(
            "ux_authz_outbox_tenant_object",
            tenant_id,
            object_id,
            unique=True,
        ),
        Index(
            "idx_authz_outbox_retry",
            next_retry_at.asc(),
        ),
    )


class EntityEmbedding(Base):
    """Stores vector embeddings for entities to support semantic search.

    Keeps dense vector weights aligned with timeseries records to facilitate high-speed
    approximate nearest neighbor queries via specialised vector indexing methods.
    """

    __tablename__ = "entity_embeddings"

    tenant_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
    )
    entity_id: Mapped[str] = mapped_column(String)
    modality: Mapped[str] = mapped_column(String)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=text("NOW()"),
    )
    metadata_: Mapped[Dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        Index(
            "idx_entity_embeddings",
            embedding,
            postgresql_using="hnsw",
            postgresql_ops={
                "embedding": "vector_cosine_ops",
            },
        ),
        Index(
            "idx_entity_embeddings_tenant_time",
            tenant_id,
            created_at.desc(),
        ),
        Index(
            "idx_entity_embeddings_tenant_entity_time",
            tenant_id,
            entity_id,
            created_at.desc(),
        ),
        Index(
            "idx_entity_embeddings_tenant_modality_time",
            tenant_id,
            modality,
            created_at.desc(),
        ),
    )
