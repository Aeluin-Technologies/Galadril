"""Contract tests for relational ontology persistence invariants."""

from __future__ import annotations

from pathlib import Path

import galadril_ontology.postgres as postgres
from galadril_ontology.schema import postgres_schema_sql


def _ontology_schema_sql() -> str:
    """Loads the shared idempotent resources that own Ontology persistence."""
    return "\n".join(postgres_schema_sql())


def test_schema_uses_relational_source_of_truth_and_composite_tenant_keys() -> (
    None
):
    normalized = " ".join(_ontology_schema_sql().lower().split())

    assert "create table if not exists ontology_revisions" in normalized
    assert "create table if not exists ontology_revision_parents" in normalized
    assert "create table if not exists ontology_branches" in normalized
    assert "create table if not exists ontology_merge_conflicts" in normalized
    assert "create table if not exists ontology_materializations" in normalized
    assert "foreign key (tenant_id, parent_revision_id)" in normalized
    assert "foreign key (tenant_id, head_revision_id)" in normalized
    assert "enable row level security" in normalized
    assert "force row level security" in normalized
    assert "app.tenant_id" in normalized
    assert (
        "revoke update, delete on ontology_base_artifacts from galadril_app"
    ) in normalized
    assert (
        "grant select, insert on ontology_base_artifacts to galadril_app"
        in normalized
    )
    assert (
        "grant select, insert on public.ontology_revisions, "
        "public.ontology_revision_parents, public.ontology_merge_conflicts, "
        "public.ontology_materializations to galadril_app" in normalized
    )
    assert (
        "grant select, insert, update on public.ontology_branches, "
        "public.ontology_catalog, public.ontology_publications, "
        "public.pipeline_ontology_bindings to galadril_app" in normalized
    )
    assert "grant select, insert, update, delete" not in normalized


def test_revision_rows_are_immutable_and_branch_heads_use_compare_and_swap() -> (
    None
):
    normalized = " ".join(_ontology_schema_sql().lower().split())

    assert "ontology_revisions_immutable" in normalized
    assert "raise exception 'ontology revisions are immutable'" in normalized
    assert "head_revision_id is not distinct from" in normalized


def test_publications_allow_retirement_but_reject_history_rewrites() -> None:
    """Publication identity and provenance remain immutable after insert."""
    normalized = " ".join(_ontology_schema_sql().lower().split())

    assert "reject_ontology_publication_rewrite" in normalized
    assert "ontology_publications_retirement_only" in normalized
    assert "old.lifecycle <> 'production'" in normalized
    assert "new.revision_id is distinct from old.revision_id" in normalized


def test_python_adapter_does_not_own_schema_ddl() -> None:
    """Keeps migration history out of import-time Python string constants."""
    source = Path(postgres.__file__).read_text(encoding="utf-8")

    assert "ONTOLOGY_SCHEMA_SQL" not in source
    assert "CREATE TABLE" not in source
