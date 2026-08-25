"""Contract tests for relational ontology persistence invariants."""

from __future__ import annotations

from galadril_ontology.postgres import ONTOLOGY_SCHEMA_SQL


def test_schema_uses_relational_source_of_truth_and_composite_tenant_keys() -> (
    None
):
    normalized = " ".join(ONTOLOGY_SCHEMA_SQL.lower().split())

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
        "revoke insert, update, delete on ontology_base_artifacts "
        "from galadril_app"
    ) in normalized
    assert (
        "grant select on ontology_base_artifacts to galadril_app" in normalized
    )


def test_revision_rows_are_immutable_and_branch_heads_use_compare_and_swap() -> (
    None
):
    normalized = " ".join(ONTOLOGY_SCHEMA_SQL.lower().split())

    assert "ontology_revisions_immutable" in normalized
    assert "raise exception 'ontology revisions are immutable'" in normalized
    assert "head_revision_id is not distinct from" in normalized
