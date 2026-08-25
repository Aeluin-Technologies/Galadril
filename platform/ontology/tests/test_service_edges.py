"""Exhaustive ontology service orchestration and error-path tests."""

from __future__ import annotations

import galadril_ontology.service as service_module
import pytest
from galadril_ontology import (
    BaseOntologyArtifact,
    BaseSynchronizationConflictError,
    BaseVersionMismatchError,
    ConcurrentHeadUpdateError,
    ConflictKind,
    InMemoryOntologyRepository,
    Ontology,
    OntologyChange,
    OntologyResource,
    OntologyRevision,
    OntologyService,
    ResourceKind,
)
from galadril_ontology.materialization import OverlayAccumulator


def _base(version: str, *, include_contract: bool = False) -> Ontology:
    resources = [
        OntologyResource(
            resource_id="core.customer",
            kind=ResourceKind.OBJECT_TYPE,
            display_name="Customer",
        ),
        OntologyResource(
            resource_id="core.customer.email",
            kind=ResourceKind.PROPERTY,
            display_name="Email",
            owner_id="core.customer",
            value_type="string",
        ),
    ]
    if include_contract:
        resources.append(
            OntologyResource(
                resource_id="tenant.contract",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Platform Contract",
            )
        )
    return Ontology(version=version, resources=tuple(resources))


async def _service(
    *, validators: tuple[service_module.OntologyValidator, ...] = ()
) -> tuple[OntologyService, InMemoryOntologyRepository]:
    repository = InMemoryOntologyRepository()
    await repository.register_base(
        BaseOntologyArtifact.from_ontology(_base("v1"))
    )
    return OntologyService(repository, validators=validators), repository


@pytest.mark.asyncio
async def test_service_validators_hash_guard_and_cache_reconstruction() -> None:
    validated: list[str] = []

    def validator(ontology: Ontology) -> None:
        validated.append(ontology.version)

    service, repository = await _service(validators=(validator,))
    root = await service.initialize_tenant("tenant-a")
    assert validated
    artifact = await repository.get_latest_base()
    invalid_revision = OntologyRevision(
        tenant_id="tenant-a",
        revision_id="f" * 32,
        base_version=artifact.version,
        base_hash="0" * 64,
        author="test",
        message="invalid",
    )
    with pytest.raises(BaseVersionMismatchError, match="base hash"):
        service._materialization(
            invalid_revision, artifact, OverlayAccumulator()
        )

    child = await service.commit(
        "tenant-a",
        "main",
        expected_head=root.head_revision_id,
        changes=(
            OntologyChange.set_field(
                "core.customer", ("description",), "Tenant customer"
            ),
        ),
        author="test",
        message="change",
    )
    repository._materializations.clear()
    reconstructed = await service.materialize("tenant-a", branch="main")
    assert reconstructed.revision_id == child.revision_id
    assert reconstructed.ontology.require("core.customer").description == (
        "Tenant customer"
    )
    branch = await service.create_branch(
        "tenant-a", "from-main", from_branch="main"
    )
    assert branch.head_revision_id == child.revision_id


@pytest.mark.asyncio
async def test_service_base_sync_stale_explicit_and_identifier_collision() -> (
    None
):
    service, repository = await _service()
    root = await service.initialize_tenant("tenant-a")
    with pytest.raises(ConcurrentHeadUpdateError):
        await service.synchronize_base(
            "tenant-a",
            "main",
            expected_head="f" * 32,
            author="test",
            message="stale",
        )

    await repository.register_base(
        BaseOntologyArtifact.from_ontology(_base("v2"))
    )
    synchronized = await service.synchronize_base(
        "tenant-a",
        "main",
        expected_head=root.head_revision_id,
        author="test",
        message="explicit",
        target_version="v2",
    )
    assert synchronized.base_version == "v2"

    custom = await service.commit(
        "tenant-a",
        "main",
        expected_head=synchronized.revision_id,
        changes=(
            OntologyChange.add_resource(
                OntologyResource(
                    resource_id="tenant.contract",
                    kind=ResourceKind.OBJECT_TYPE,
                    display_name="Tenant Contract",
                )
            ),
        ),
        author="test",
        message="custom",
    )
    await repository.register_base(
        BaseOntologyArtifact.from_ontology(_base("v3", include_contract=True))
    )
    with pytest.raises(BaseSynchronizationConflictError) as captured:
        await service.synchronize_base(
            "tenant-a",
            "main",
            expected_head=custom.revision_id,
            author="test",
            message="collision",
        )
    assert captured.value.conflicts[0].kind is ConflictKind.ADD_ADD


@pytest.mark.asyncio
async def test_service_merge_stale_and_base_mismatch_paths() -> None:
    service, repository = await _service()
    root = await service.initialize_tenant("tenant-a")
    await service.create_branch("tenant-a", "experiment")
    with pytest.raises(ConcurrentHeadUpdateError):
        await service.merge(
            "tenant-a",
            target_branch="main",
            source_branch="experiment",
            expected_target_head="f" * 32,
            author="test",
            message="stale",
        )

    await repository.register_base(
        BaseOntologyArtifact.from_ontology(_base("v2"))
    )
    await service.synchronize_base(
        "tenant-a",
        "experiment",
        expected_head=root.head_revision_id,
        author="test",
        message="sync experiment",
    )
    with pytest.raises(BaseVersionMismatchError, match="synchronize"):
        await service.merge(
            "tenant-a",
            target_branch="main",
            source_branch="experiment",
            expected_target_head=root.head_revision_id,
            author="test",
            message="mismatch",
        )


@pytest.mark.asyncio
async def test_service_merge_defensive_and_invalid_result_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = await _service()
    root = await service.initialize_tenant("tenant-a")
    await service.create_branch("tenant-a", "experiment")

    monkeypatch.setattr(
        service_module, "three_way_merge", lambda *args, **kwargs: (None, ())
    )
    with pytest.raises(RuntimeError, match="neither result"):
        await service.merge(
            "tenant-a",
            target_branch="main",
            source_branch="experiment",
            expected_target_head=root.head_revision_id,
            author="test",
            message="defensive",
        )

    invalid = Ontology(
        version="v1",
        resources=(
            OntologyResource(
                resource_id="core.customer",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Customer",
                references=("core.missing",),
            ),
        ),
    )
    monkeypatch.setattr(
        service_module,
        "three_way_merge",
        lambda *args, **kwargs: (invalid, ()),
    )
    result = await service.merge(
        "tenant-a",
        target_branch="main",
        source_branch="experiment",
        expected_target_head=root.head_revision_id,
        author="test",
        message="invalid",
    )
    assert result.conflicts[0].kind is ConflictKind.INVALID_RESULT
