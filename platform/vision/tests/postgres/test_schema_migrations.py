"""Contract tests for shared PostgreSQL migrations and ORM mappings."""

from __future__ import annotations

from pathlib import Path

import galadril_vision.connectors.postgres.client as client_module
from galadril_ontology.schema import postgres_schema_sql
from galadril_vision.connectors.postgres.models import Base
from galadril_vision.connectors.postgres.schema import vision_security_sql


def _gateway_migrations() -> tuple[Path, ...]:
    """Returns Gateway SQLx migrations in their execution order."""
    return tuple(
        sorted(Path("schemas/postgres/gateway_migrations").glob("*.sql"))
    )


def test_migration_files_are_ordered_and_replace_monolithic_schema() -> None:
    """Uses append-only SQLx migration files as the only schema history."""
    migrations = _gateway_migrations()

    assert migrations
    assert not Path("schemas/postgres/gateway.sql").exists()
    assert [path.name for path in migrations] == sorted(
        path.name for path in migrations
    )
    assert len({path.name.split("_", 1)[0] for path in migrations}) == len(
        migrations
    )


def test_vision_security_covers_every_sqlalchemy_table() -> None:
    """Keeps ORM-created tables behind the shared fail-closed RLS boundary."""
    security_sql = "\n".join(vision_security_sql())

    for table_name in Base.metadata.tables:
        assert f"'{table_name}'" in security_sql


def test_shared_ontology_creation_is_idempotent() -> None:
    """Allows every Vision/Ontology host to initialize the same schema safely."""
    schema_sql = "\n".join(postgres_schema_sql())

    assert "CREATE TABLE IF NOT EXISTS ontology_revisions" in schema_sql
    assert "CREATE EXTENSION IF NOT EXISTS age" in schema_sql
    assert "DROP POLICY IF EXISTS tenant_isolation" in schema_sql


def test_all_binary_owned_table_creation_is_idempotent() -> None:
    """Guards every binary-owned relation and trigger creation path."""
    sql_resources = [
        path.read_text(encoding="utf-8") for path in _gateway_migrations()
    ]
    sql_resources.extend(postgres_schema_sql())
    schema_sql = "\n".join(sql_resources)

    for statement in sql_resources:
        assert "CREATE TABLE " not in statement.replace(
            "CREATE TABLE IF NOT EXISTS ", ""
        )
        assert "CREATE INDEX " not in statement.replace(
            "CREATE INDEX IF NOT EXISTS ", ""
        )

    for trigger_name in (
        "audit_events_immutable",
        "conversation_message_revisions_immutable",
        "pipeline_revisions_immutable",
        "ontology_revisions_immutable",
        "ontology_revision_parents_immutable",
        "ontology_base_artifacts_immutable",
        "ontology_publications_retirement_only",
    ):
        assert f"DROP TRIGGER IF EXISTS {trigger_name}" in schema_sql
        assert f"CREATE TRIGGER {trigger_name}" in schema_sql

    assert "CREATE OR REPLACE FUNCTION" in schema_sql
    assert "DROP POLICY IF EXISTS tenant_isolation" in schema_sql


def test_database_images_provision_constrained_schema_owner() -> None:
    """Keeps startup DDL available without superuser or RLS bypass rights."""
    sources = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "database/docker-entrypoint-initdb.d/004-create-galadril-app.sh",
            "infrastructure/docker/init-scripts/02-init-galadril-roles.sh",
        )
    )

    assert "NOSUPERUSER NOBYPASSRLS" in sources
    assert "GRANT USAGE, CREATE ON SCHEMA public TO galadril_app" in sources
    assert (
        "galadril_maintenance LOGIN NOINHERIT NOSUPERUSER BYPASSRLS" in sources
    )
    assert "GRANT SELECT, UPDATE, DELETE ON authz_outbox" not in sources
    assert "SUPERUSER PASSWORD" not in sources


def test_vision_client_contains_no_inline_table_ddl() -> None:
    """Prevents schema creation from drifting back into application globals."""
    source = Path(client_module.__file__).read_text(encoding="utf-8")

    assert "CREATE TABLE" not in source
    assert source.count("metadata.create_all") == 1
    assert "SELECT 1 FROM ag_catalog.ag_graph" in source
    assert "_ensure_schema_invariants" not in source
