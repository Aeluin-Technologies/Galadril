"""Behavioral tests for tenant ontology history and materialization."""

from __future__ import annotations

import asyncio

import pytest
from galadril_ontology import (
    BaseOntologyArtifact,
    BaseSynchronizationConflictError,
    ConcurrentHeadUpdateError,
    ConflictKind,
    InMemoryOntologyRepository,
    Ontology,
    OntologyChange,
    OntologyNotFoundError,
    OntologyResource,
    OntologyService,
    ResourceKind,
)


def _resource(
    resource_id: str,
    *,
    description: str = "",
    owner_id: str | None = None,
    value_type: str | None = None,
    references: tuple[str, ...] = (),
) -> OntologyResource:
    kind = (
        ResourceKind.PROPERTY
        if owner_id is not None
        else ResourceKind.OBJECT_TYPE
    )
    return OntologyResource(
        resource_id=resource_id,
        kind=kind,
        display_name=resource_id.rsplit(".", 1)[-1].title(),
        description=description,
        owner_id=owner_id,
        value_type=value_type,
        references=references,
    )


def _base(version: str, *, name_description: str = "Legal name") -> Ontology:
    return Ontology(
        resources=(
            _resource("core.customer", description="A customer"),
            _resource(
                "core.customer.name",
                description=name_description,
                owner_id="core.customer",
                value_type="string",
            ),
            _resource(
                "core.customer.email",
                description="Primary email",
                owner_id="core.customer",
                value_type="string",
            ),
            _resource(
                "core.customer.country",
                description="Country code",
                owner_id="core.customer",
                value_type="string",
            ),
        ),
        version=version,
    )


async def _service() -> tuple[OntologyService, InMemoryOntologyRepository]:
    repository = InMemoryOntologyRepository()
    await repository.register_base(
        BaseOntologyArtifact.from_ontology(_base("vision-1"))
    )
    return OntologyService(repository), repository


@pytest.mark.asyncio
async def test_tenants_inherit_same_base_and_changes_remain_isolated() -> None:
    service, _ = await _service()
    tenant_a = await service.initialize_tenant("tenant-a")
    tenant_b = await service.initialize_tenant("tenant-b")

    initial_a = await service.materialize("tenant-a", tenant_a.head_revision_id)
    initial_b = await service.materialize("tenant-b", tenant_b.head_revision_id)
    assert initial_a.ontology == initial_b.ontology

    revision = await service.commit(
        "tenant-a",
        "main",
        expected_head=tenant_a.head_revision_id,
        changes=(
            OntologyChange.set_field(
                "core.customer.email",
                ("description",),
                "Primary billing email",
            ),
        ),
        author="alice",
        message="Customize billing semantics",
    )

    effective_a = await service.materialize("tenant-a", revision.revision_id)
    effective_b = await service.materialize(
        "tenant-b", tenant_b.head_revision_id
    )
    assert (
        effective_a.ontology.require("core.customer.email").description
        == "Primary billing email"
    )
    assert (
        effective_b.ontology.require("core.customer.email").description
        == "Primary email"
    )
    semantic_diff = await service.diff(
        "tenant-a", tenant_a.head_revision_id, revision.revision_id
    )
    assert semantic_diff == revision.changes


@pytest.mark.asyncio
async def test_add_suppress_and_restore_are_sparse_tenant_overrides() -> None:
    service, _ = await _service()
    tenant_a = await service.initialize_tenant("tenant-a")
    tenant_b = await service.initialize_tenant("tenant-b")
    custom = _resource("tenant.custom_contract", description="Private contract")

    changed = await service.commit(
        "tenant-a",
        "main",
        expected_head=tenant_a.head_revision_id,
        changes=(
            OntologyChange.add_resource(custom),
            OntologyChange.remove_resource("core.customer.country"),
        ),
        author="alice",
        message="Add contracts and hide country",
    )

    effective_a = await service.materialize("tenant-a", changed.revision_id)
    effective_b = await service.materialize(
        "tenant-b", tenant_b.head_revision_id
    )
    assert effective_a.ontology.get("tenant.custom_contract") == custom
    assert effective_a.ontology.get("core.customer.country") is None
    assert effective_b.ontology.get("tenant.custom_contract") is None
    assert effective_b.ontology.get("core.customer.country") is not None

    restored = await service.commit(
        "tenant-a",
        "main",
        expected_head=changed.revision_id,
        changes=(
            OntologyChange.restore_resource("core.customer.country"),
            OntologyChange.restore_resource("tenant.custom_contract"),
        ),
        author="alice",
        message="Restore platform defaults",
    )
    effective_restored = await service.materialize(
        "tenant-a", restored.revision_id
    )
    assert effective_restored.ontology.get("core.customer.country") is not None
    assert effective_restored.ontology.get("tenant.custom_contract") is None


@pytest.mark.asyncio
async def test_restore_field_and_base_sync_preserve_sparse_inheritance() -> (
    None
):
    service, repository = await _service()
    branch = await service.initialize_tenant("tenant-a")
    customized = await service.commit(
        "tenant-a",
        "main",
        expected_head=branch.head_revision_id,
        changes=(
            OntologyChange.set_field(
                "core.customer.email",
                ("description",),
                "Tenant email",
            ),
        ),
        author="alice",
        message="Customize email",
    )
    version_two = _base("vision-2", name_description="Registered name")
    await repository.register_base(
        BaseOntologyArtifact.from_ontology(version_two)
    )

    synchronized = await service.synchronize_base(
        "tenant-a",
        "main",
        expected_head=customized.revision_id,
        author="platform",
        message="Synchronize Vision ontology",
    )
    effective = await service.materialize("tenant-a", synchronized.revision_id)
    assert effective.base_version == "vision-2"
    assert (
        effective.ontology.require("core.customer.name").description
        == "Registered name"
    )
    assert (
        effective.ontology.require("core.customer.email").description
        == "Tenant email"
    )

    restored = await service.commit(
        "tenant-a",
        "main",
        expected_head=synchronized.revision_id,
        changes=(
            OntologyChange.restore_field(
                "core.customer.email", ("description",)
            ),
        ),
        author="alice",
        message="Inherit email again",
    )
    assert (
        await service.materialize("tenant-a", restored.revision_id)
    ).ontology.require("core.customer.email").description == "Primary email"


@pytest.mark.asyncio
async def test_base_sync_reports_removed_overridden_resource() -> None:
    service, repository = await _service()
    branch = await service.initialize_tenant("tenant-a")
    customized = await service.commit(
        "tenant-a",
        "main",
        expected_head=branch.head_revision_id,
        changes=(
            OntologyChange.set_field(
                "core.customer.email", ("description",), "Tenant email"
            ),
        ),
        author="alice",
        message="Customize email",
    )
    version_two = Ontology(
        version="vision-2",
        resources=tuple(
            resource
            for resource in _base("vision-2").resources
            if resource.resource_id != "core.customer.email"
        ),
    )
    await repository.register_base(
        BaseOntologyArtifact.from_ontology(version_two)
    )

    with pytest.raises(BaseSynchronizationConflictError) as error:
        await service.synchronize_base(
            "tenant-a",
            "main",
            expected_head=customized.revision_id,
            author="platform",
            message="Synchronize Vision ontology",
        )

    conflict = error.value.conflicts[0]
    assert conflict.kind is ConflictKind.BASE_RESOURCE_REMOVED
    assert conflict.resource_id == "core.customer.email"


@pytest.mark.asyncio
async def test_branches_are_refs_and_independent_changes_merge() -> None:
    service, _ = await _service()
    main = await service.initialize_tenant("tenant-a")
    experiment = await service.create_branch(
        "tenant-a", "experiment", from_revision=main.head_revision_id
    )
    assert experiment.head_revision_id == main.head_revision_id

    main_revision = await service.commit(
        "tenant-a",
        "main",
        expected_head=main.head_revision_id,
        changes=(
            OntologyChange.set_field(
                "core.customer.email",
                ("description",),
                "Billing email",
            ),
        ),
        author="alice",
        message="Clarify email",
    )
    experiment_revision = await service.commit(
        "tenant-a",
        "experiment",
        expected_head=experiment.head_revision_id,
        changes=(
            OntologyChange.add_resource(
                _resource(
                    "core.customer.phone",
                    owner_id="core.customer",
                    value_type="string",
                )
            ),
        ),
        author="bob",
        message="Add phone",
    )

    assert (
        await service.materialize("tenant-a", main_revision.revision_id)
    ).ontology.get("core.customer.phone") is None
    result = await service.merge(
        "tenant-a",
        target_branch="main",
        source_branch="experiment",
        expected_target_head=main_revision.revision_id,
        author="alice",
        message="Merge experiment",
    )
    assert result.conflicts == ()
    assert result.revision is not None
    assert result.revision.parents == (
        main_revision.revision_id,
        experiment_revision.revision_id,
    )
    assert (
        await service.merge_base(
            "tenant-a",
            main_revision.revision_id,
            experiment_revision.revision_id,
        )
        == main.head_revision_id
    )
    merged = await service.materialize("tenant-a", result.revision.revision_id)
    assert merged.ontology.get("core.customer.phone") is not None
    assert (
        merged.ontology.require("core.customer.email").description
        == "Billing email"
    )


@pytest.mark.asyncio
async def test_conflicting_edits_return_structured_conflict() -> None:
    service, repository = await _service()
    main = await service.initialize_tenant("tenant-a")
    branch = await service.create_branch(
        "tenant-a", "experiment", from_revision=main.head_revision_id
    )
    left = await service.commit(
        "tenant-a",
        "main",
        expected_head=main.head_revision_id,
        changes=(
            OntologyChange.set_field(
                "core.customer.email",
                ("description",),
                "Personal email",
            ),
        ),
        author="alice",
        message="Personal email",
    )
    await service.commit(
        "tenant-a",
        "experiment",
        expected_head=branch.head_revision_id,
        changes=(
            OntologyChange.set_field(
                "core.customer.email",
                ("description",),
                "Billing email",
            ),
        ),
        author="bob",
        message="Billing email",
    )

    result = await service.merge(
        "tenant-a",
        target_branch="main",
        source_branch="experiment",
        expected_target_head=left.revision_id,
        author="alice",
        message="Attempt merge",
    )

    assert result.revision is None
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.kind is ConflictKind.FIELD_VALUE
    assert conflict.resource_id == "core.customer.email"
    assert conflict.path == ("description",)
    assert conflict.base.value == "Primary email"
    assert conflict.left.value == "Personal email"
    assert conflict.right.value == "Billing email"
    assert await repository.list_conflicts("tenant-a", result.merge_id) == (
        conflict,
    )


@pytest.mark.asyncio
async def test_deletion_versus_modification_is_a_conflict() -> None:
    service, _ = await _service()
    main = await service.initialize_tenant("tenant-a")
    branch = await service.create_branch(
        "tenant-a", "experiment", from_revision=main.head_revision_id
    )
    deleted = await service.commit(
        "tenant-a",
        "main",
        expected_head=main.head_revision_id,
        changes=(OntologyChange.remove_resource("core.customer.email"),),
        author="alice",
        message="Suppress email",
    )
    await service.commit(
        "tenant-a",
        "experiment",
        expected_head=branch.head_revision_id,
        changes=(
            OntologyChange.set_field(
                "core.customer.email",
                ("description",),
                "Billing email",
            ),
        ),
        author="bob",
        message="Modify email",
    )

    result = await service.merge(
        "tenant-a",
        target_branch="main",
        source_branch="experiment",
        expected_target_head=deleted.revision_id,
        author="alice",
        message="Attempt merge",
    )
    assert result.revision is None
    assert result.conflicts[0].kind is ConflictKind.DELETE_MODIFY


@pytest.mark.asyncio
async def test_merge_does_not_expand_parent_tombstone_to_owned_resources() -> (
    None
):
    service, _ = await _service()
    main = await service.initialize_tenant("tenant-a")
    suppressed = await service.commit(
        "tenant-a",
        "main",
        expected_head=main.head_revision_id,
        changes=(OntologyChange.remove_resource("core.customer"),),
        author="alice",
        message="Suppress customer",
    )
    experiment = await service.create_branch(
        "tenant-a", "experiment", from_revision=suppressed.revision_id
    )
    left = await service.commit(
        "tenant-a",
        "main",
        expected_head=suppressed.revision_id,
        changes=(
            OntologyChange.add_resource(_resource("tenant.left_resource")),
        ),
        author="alice",
        message="Add left resource",
    )
    await service.commit(
        "tenant-a",
        "experiment",
        expected_head=experiment.head_revision_id,
        changes=(
            OntologyChange.add_resource(_resource("tenant.right_resource")),
        ),
        author="bob",
        message="Add right resource",
    )
    result = await service.merge(
        "tenant-a",
        target_branch="main",
        source_branch="experiment",
        expected_target_head=left.revision_id,
        author="alice",
        message="Merge hidden customer branches",
    )
    assert result.revision is not None

    restored = await service.commit(
        "tenant-a",
        "main",
        expected_head=result.revision.revision_id,
        changes=(OntologyChange.restore_resource("core.customer"),),
        author="alice",
        message="Restore customer",
    )
    effective = await service.materialize("tenant-a", restored.revision_id)
    assert effective.ontology.get("core.customer.email") is not None
    assert effective.ontology.get("core.customer.country") is not None


@pytest.mark.asyncio
async def test_history_is_reproducible_and_materializations_are_cached() -> (
    None
):
    service, repository = await _service()
    initial = await service.initialize_tenant("tenant-a")
    original = await service.materialize("tenant-a", initial.head_revision_id)
    writes_after_first_read = repository.materialization_write_count

    again = await service.materialize("tenant-a", initial.head_revision_id)
    assert again == original
    assert repository.materialization_write_count == writes_after_first_read

    await repository.register_base(
        BaseOntologyArtifact.from_ontology(
            _base("vision-2", name_description="New name")
        )
    )
    historical = await service.materialize("tenant-a", initial.head_revision_id)
    assert historical.base_version == "vision-1"
    assert historical.base_hash == original.base_hash
    assert historical.ontology == original.ontology


@pytest.mark.asyncio
async def test_branch_head_compare_and_swap_rejects_stale_writer() -> None:
    service, _ = await _service()
    main = await service.initialize_tenant("tenant-a")

    async def commit(description: str) -> str:
        revision = await service.commit(
            "tenant-a",
            "main",
            expected_head=main.head_revision_id,
            changes=(
                OntologyChange.set_field(
                    "core.customer.email", ("description",), description
                ),
            ),
            author=description,
            message=description,
        )
        return revision.revision_id

    results = await asyncio.gather(
        commit("Writer one"), commit("Writer two"), return_exceptions=True
    )
    assert sum(isinstance(value, str) for value in results) == 1
    assert (
        sum(isinstance(value, ConcurrentHeadUpdateError) for value in results)
        == 1
    )


@pytest.mark.asyncio
async def test_cross_tenant_revision_references_fail_closed() -> None:
    service, _ = await _service()
    await service.initialize_tenant("tenant-a")
    tenant_b = await service.initialize_tenant("tenant-b")

    with pytest.raises(OntologyNotFoundError):
        await service.materialize("tenant-a", tenant_b.head_revision_id)
    with pytest.raises(OntologyNotFoundError):
        await service.create_branch(
            "tenant-a", "stolen", from_revision=tenant_b.head_revision_id
        )
