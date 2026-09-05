"""Unit tests validating SQLAlchemy structural schemas, indexes, and type mappings."""

from datetime import UTC, datetime

import pytest
from galadril_vision.connectors.postgres.models import (
    AuthzOutbox,
    Base,
    CausalRun,
    EntityEmbedding,
    EntityState,
    EskgEvent,
    IdentityLink,
    PipelineExecution,
)
from sqlalchemy import CheckConstraint, Index


def test_entity_state_schema_attributes() -> None:
    """Validates structural constraints and definitions for EntityState."""
    instance = EntityState(
        tenant_id="tenant-1",
        entity_id="entity-abc",
        event_id="ev-123",
        state_type="thermal_override",
        state_value={"celsius": 42.5},
        event_time=datetime.now(UTC),
    )

    assert instance.tenant_id == "tenant-1"
    assert instance.entity_id == "entity-abc"
    assert instance.state_value["celsius"] == 42.5
    assert EntityState.__tablename__ == "entity_states"

    table_args = EntityState.__table_args__
    index_names = [arg.name for arg in table_args if isinstance(arg, Index)]
    assert "idx_entity_states_tenant_entity_time" in index_names
    assert "idx_entity_states_geom" in index_names
    assert "idx_entity_states_name_trgm" in index_names


def test_eskg_event_schema_attributes() -> None:
    """Validates structural constraints and definitions for EskgEvent."""
    instance = EskgEvent(
        tenant_id="tenant-9",
        event_id="evt-77",
        event_type="CRITICAL_SHUTDOWN",
        event_time=datetime.now(UTC),
        properties={"node": "compute-01"},
    )

    assert instance.event_type == "CRITICAL_SHUTDOWN"
    assert instance.properties["node"] == "compute-01"
    assert EskgEvent.__tablename__ == "eskg_events"

    table_args = EskgEvent.__table_args__
    index_names = [arg.name for arg in table_args if isinstance(arg, Index)]
    assert "idx_eskg_events_tenant_type_time" in index_names


def test_causal_run_schema_attributes() -> None:
    """Validates structural constraints and definitions for CausalRun."""
    instance = CausalRun(
        cache_key="hash_sig_99",
        target="metric_anomaly_ratio",
        window_start=datetime.now(UTC),
        window_end=datetime.now(UTC),
        status="COMPLETED",
        result_summary={"p_value": 0.001},
    )

    assert instance.cache_key == "hash_sig_99"
    assert instance.result_summary["p_value"] == 0.001
    assert CausalRun.__tablename__ == "causal_runs"

    table_args = CausalRun.__table_args__
    index_names = [arg.name for arg in table_args if isinstance(arg, Index)]
    assert "idx_causal_runs_window" in index_names
    assert "idx_causal_runs_target" in index_names


def test_authz_outbox_schema_constraints() -> None:
    """Validates structural constraints, default constraints, and inline definitions for AuthzOutbox."""
    instance = AuthzOutbox(
        tenant_id="t-1",
        object_id="obj-99",
        tuples_json=[{"user_id": "u1", "role": "viewer"}],
    )

    assert instance.tenant_id == "t-1"
    assert AuthzOutbox.__tablename__ == "authz_outbox"

    table_args = AuthzOutbox.__table_args__

    index_names = [arg.name for arg in table_args if isinstance(arg, Index)]
    assert "ux_authz_outbox_tenant_object" in index_names
    assert "idx_authz_outbox_retry" in index_names

    constraint_names = [
        arg.name for arg in table_args if isinstance(arg, CheckConstraint)
    ]
    assert "ck_authz_outbox_tenant_nonempty" in constraint_names
    assert "ck_authz_outbox_object_nonempty" in constraint_names
    assert "ck_authz_outbox_tuples_array" in constraint_names


def test_pipeline_execution_schema_constraints() -> None:
    """Validates the durable command lease schema and bounded status values."""
    now = datetime.now(UTC)
    instance = PipelineExecution(
        idempotency_key="command:vision:infer",
        command_id="command",
        correlation_id="correlation",
        tenant_id="tenant-1",
        pipeline="vision",
        step="infer",
        status="running",
        attempt=0,
        lease_expires_at=now,
    )

    assert instance.idempotency_key == "command:vision:infer"
    assert instance.lease_expires_at is now
    assert PipelineExecution.__tablename__ == "pipeline_executions"

    table_args = PipelineExecution.__table_args__
    index_names = [arg.name for arg in table_args if isinstance(arg, Index)]
    assert "idx_pipeline_executions_correlation" in index_names
    assert "idx_pipeline_executions_lease" in index_names
    constraint_names = [
        arg.name for arg in table_args if isinstance(arg, CheckConstraint)
    ]
    assert "ck_pipeline_executions_status" in constraint_names


def test_entity_embedding_schema_attributes() -> None:
    """Validates structural constraints and definitions for EntityEmbedding."""
    instance = EntityEmbedding(
        tenant_id="tenant-core",
        id="emb-001",
        entity_id="ent-88",
        modality="TEXT",
        embedding=[0.1] * 1024,
        metadata_={"source": "transformer_v4"},
    )

    assert instance.modality == "TEXT"
    assert len(instance.embedding) == 1024
    assert instance.metadata_["source"] == "transformer_v4"
    assert EntityEmbedding.__tablename__ == "entity_embeddings"

    table_args = EntityEmbedding.__table_args__
    index_names = [arg.name for arg in table_args if isinstance(arg, Index)]
    assert "idx_entity_embeddings" in index_names
    assert "idx_entity_embeddings_tenant_time" in index_names
    assert "idx_entity_embeddings_tenant_entity_time" in index_names
    assert "idx_entity_embeddings_tenant_modality_time" in index_names


def test_identity_link_schema_enforces_tenant_scoped_bijection() -> None:
    """Keeps PostgreSQL and LI-ESKG identifiers stable in both directions."""
    link = IdentityLink(
        tenant_id="tenant-core",
        entity_id="person-1",
        licorne_identity_id=42,
        licorne_version=7,
    )

    assert link.licorne_identity_id == 42
    constraint_names = [
        arg.name
        for arg in IdentityLink.__table_args__
        if isinstance(arg, CheckConstraint)
    ]
    assert "ck_identity_links_licorne_id_nonnegative" in constraint_names


def test_metadata_registry_integrity() -> None:
    """Ensures all mapping models are correctly bound to the central registry class."""
    registered_tables = Base.metadata.tables.keys()
    assert "entity_states" in registered_tables
    assert "eskg_events" in registered_tables
    assert "causal_runs" in registered_tables
    assert "pipeline_executions" in registered_tables
    assert "authz_outbox" in registered_tables
    assert "entity_embeddings" in registered_tables
    assert "identity_links" in registered_tables


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
