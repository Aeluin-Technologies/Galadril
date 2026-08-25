"""Runtime ontology publication, slicing, and context tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from galadril_ontology import (
    BlockOntologyContract,
    InMemoryOntologyRuntimeStore,
    MaterializedOntology,
    Ontology,
    OntologyResource,
    OntologyRuntimeManager,
    OntologySlice,
    OntologySliceRequest,
    OntologySliceSelector,
    OverlaySnapshot,
    PipelineOntologyBinding,
    PublishedOntology,
    ResourceKind,
    active_ontology_slice,
)
from galadril_ontology.errors import (
    OntologyCompatibilityError,
    OntologyNotFoundError,
)
from galadril_ontology.model import ontology_content_hash
from pydantic import TypeAdapter, ValidationError


def _materialization(
    tenant_id: str,
    revision_id: str,
    *,
    description: str,
) -> MaterializedOntology:
    ontology = Ontology(
        version="vision-1",
        resources=(
            OntologyResource(
                resource_id="core.customer",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Customer",
                description=description,
            ),
            OntologyResource(
                resource_id="core.customer.email",
                kind=ResourceKind.PROPERTY,
                display_name="Email",
                owner_id="core.customer",
                value_type="string",
            ),
            OntologyResource(
                resource_id="core.invoice",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Invoice",
            ),
        ),
    )
    return MaterializedOntology(
        tenant_id=tenant_id,
        revision_id=revision_id,
        base_version="vision-1",
        base_hash="a" * 64,
        effective_hash=ontology_content_hash(ontology),
        overlay=OverlaySnapshot(),
        ontology=ontology,
    )


def _publication(
    tenant_id: str,
    ontology_id: str,
    revision_id: str,
    *,
    description: str,
) -> PublishedOntology:
    return PublishedOntology(
        tenant_id=tenant_id,
        ontology_id=ontology_id,
        publication_id=(revision_id[0] * 32),
        materialization=_materialization(
            tenant_id, revision_id, description=description
        ),
        metadata={"environment": "production"},
        published_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_runtime_resolves_latest_publication_and_dependency_slice() -> (
    None
):
    """Reloads production state and includes owners required by selected fields."""
    store = InMemoryOntologyRuntimeStore()
    await store.publish(
        _publication("tenant-a", "sales", "1" * 32, description="v1")
    )
    await store.bind(
        PipelineOntologyBinding(
            tenant_id="tenant-a",
            pipeline_id="customer-pipeline",
            block_id="resolve",
            ontology_id="sales",
            selector=OntologySliceSelector(
                resource_ids=("core.customer.email",),
                include_dependencies=True,
            ),
            metadata={"purpose": "identity"},
        )
    )
    manager = OntologyRuntimeManager(store)
    request = OntologySliceRequest(
        tenant_id="tenant-a",
        pipeline_id="customer-pipeline",
        block_id="resolve",
    )

    first = await manager.resolve(request)
    await store.publish(
        _publication("tenant-a", "sales", "2" * 32, description="v2")
    )
    second = await manager.resolve(request)

    assert tuple(item.resource_id for item in first.ontology.resources) == (
        "core.customer",
        "core.customer.email",
    )
    assert first.ontology.require("core.customer").description == "v1"
    assert second.ontology.require("core.customer").description == "v2"
    assert second.metadata.publication_metadata == {"environment": "production"}
    assert second.metadata.binding_metadata == {"purpose": "identity"}


@pytest.mark.asyncio
async def test_runtime_isolates_multiple_tenants_pipelines_and_ontologies() -> (
    None
):
    """Scopes every lookup by tenant, pipeline, block, and ontology identity."""
    store = InMemoryOntologyRuntimeStore()
    for tenant_id, ontology_id, revision_id in (
        ("tenant-a", "sales", "1" * 32),
        ("tenant-a", "risk", "2" * 32),
        ("tenant-b", "sales", "3" * 32),
    ):
        await store.publish(
            _publication(
                tenant_id,
                ontology_id,
                revision_id,
                description=f"{tenant_id}:{ontology_id}",
            )
        )
    for pipeline_id, ontology_id, resource_id in (
        ("sales-pipeline", "sales", "core.customer"),
        ("risk-pipeline", "risk", "core.invoice"),
    ):
        await store.bind(
            PipelineOntologyBinding(
                tenant_id="tenant-a",
                pipeline_id=pipeline_id,
                block_id="sink",
                ontology_id=ontology_id,
                selector=OntologySliceSelector(resource_ids=(resource_id,)),
            )
        )
    manager = OntologyRuntimeManager(store)

    sales = await manager.resolve(
        OntologySliceRequest(
            tenant_id="tenant-a",
            pipeline_id="sales-pipeline",
            block_id="sink",
        )
    )
    risk = await manager.resolve(
        OntologySliceRequest(
            tenant_id="tenant-a",
            pipeline_id="risk-pipeline",
            block_id="sink",
        )
    )

    assert sales.metadata.ontology_id == "sales"
    assert risk.metadata.ontology_id == "risk"
    assert tuple(item.resource_id for item in risk.ontology.resources) == (
        "core.invoice",
    )
    with pytest.raises(OntologyNotFoundError):
        await manager.resolve(
            OntologySliceRequest(
                tenant_id="tenant-b",
                pipeline_id="sales-pipeline",
                block_id="sink",
            )
        )


@pytest.mark.asyncio
async def test_runtime_validates_contract_and_binds_active_context() -> None:
    """Rejects incompatible slices and resets task-local state after execution."""
    store = InMemoryOntologyRuntimeStore()
    await store.publish(
        _publication("tenant-a", "sales", "1" * 32, description="v1")
    )
    await store.bind(
        PipelineOntologyBinding(
            tenant_id="tenant-a",
            pipeline_id="pipeline",
            block_id="block",
            ontology_id="sales",
            selector=OntologySliceSelector(
                resource_ids=("core.customer.email",),
                include_dependencies=True,
            ),
        )
    )
    manager = OntologyRuntimeManager(store)
    request = OntologySliceRequest(
        tenant_id="tenant-a", pipeline_id="pipeline", block_id="block"
    )
    contract = BlockOntologyContract(
        required_resource_ids=("core.customer.email",),
        allowed_kinds=(ResourceKind.OBJECT_TYPE, ResourceKind.PROPERTY),
    )

    assert active_ontology_slice() is None
    async with manager.bind(request, contract) as bound:
        assert active_ontology_slice() is bound
        assert bound.metadata.revision_id == "1" * 32
    assert active_ontology_slice() is None

    with pytest.raises(OntologyCompatibilityError, match="required resource"):
        await manager.resolve(
            request,
            BlockOntologyContract(
                required_resource_ids=("core.missing",),
            ),
        )
    with pytest.raises(OntologyCompatibilityError, match="resource kind"):
        await manager.resolve(
            request,
            BlockOntologyContract(allowed_kinds=(ResourceKind.EVENT_TYPE,)),
        )


def test_runtime_schema_serialization_and_selector_validation() -> None:
    """Round-trips runtime contracts without losing immutable metadata."""
    publication = _publication(
        "tenant-a", "sales", "1" * 32, description="serialized"
    )
    binding = PipelineOntologyBinding(
        tenant_id="tenant-a",
        pipeline_id="pipeline",
        block_id="block",
        ontology_id="sales",
        selector=OntologySliceSelector(kinds=(ResourceKind.OBJECT_TYPE,)),
    )
    store = InMemoryOntologyRuntimeStore()

    assert (
        PublishedOntology.model_validate_json(publication.model_dump_json())
        == publication
    )
    assert (
        PipelineOntologyBinding.model_validate_json(binding.model_dump_json())
        == binding
    )
    assert TypeAdapter(OntologySlice | None).validate_json("null") is None
    assert store.publication_count == 0
    with pytest.raises(ValidationError, match="selector"):
        OntologySliceSelector()


@pytest.mark.asyncio
async def test_runtime_rejects_invalid_identifiers_scopes_and_missing_state() -> (
    None
):
    """Covers every fail-closed runtime catalog and binding transition."""
    for constructor in (
        lambda: OntologySliceRequest(
            tenant_id="tenant-a", pipeline_id="bad id", block_id="block"
        ),
        lambda: OntologySliceSelector(
            resource_ids=("core.customer", "core.customer")
        ),
        lambda: OntologySliceSelector(
            kinds=(ResourceKind.OBJECT_TYPE, ResourceKind.OBJECT_TYPE)
        ),
    ):
        with pytest.raises(ValidationError):
            constructor()
    publication = _publication("tenant-a", "sales", "1" * 32, description="v1")
    with pytest.raises(ValidationError, match="tenants differ"):
        PublishedOntology(
            tenant_id="tenant-b",
            ontology_id="sales",
            publication_id="1" * 32,
            materialization=publication.materialization,
        )

    store = InMemoryOntologyRuntimeStore()
    binding = PipelineOntologyBinding(
        tenant_id="tenant-a",
        pipeline_id="pipeline",
        block_id="block",
        ontology_id="sales",
        selector=OntologySliceSelector(resource_ids=("core.customer",)),
    )
    with pytest.raises(OntologyNotFoundError, match="not published"):
        await store.bind(binding)
    await store.publish(publication)
    await store.bind(binding)
    store._publications.clear()
    with pytest.raises(OntologyNotFoundError, match="not published"):
        await store.load_runtime_slice(
            OntologySliceRequest(
                tenant_id="tenant-a", pipeline_id="pipeline", block_id="block"
            )
        )
    assert store.load_count == 1


@pytest.mark.asyncio
async def test_runtime_rejects_wrong_tenant_and_incomplete_dependency_slice() -> (
    None
):
    """Validates store provenance and dependency closure before block execution."""
    store = InMemoryOntologyRuntimeStore()
    await store.publish(
        _publication("tenant-a", "sales", "1" * 32, description="v1")
    )
    await store.bind(
        PipelineOntologyBinding(
            tenant_id="tenant-a",
            pipeline_id="pipeline",
            block_id="block",
            ontology_id="sales",
            selector=OntologySliceSelector(
                resource_ids=("core.customer.email",),
                include_dependencies=False,
            ),
        )
    )
    request = OntologySliceRequest(
        tenant_id="tenant-a", pipeline_id="pipeline", block_id="block"
    )
    with pytest.raises(OntologyCompatibilityError, match="dependency closure"):
        await OntologyRuntimeManager(store).resolve(request)

    valid_store = InMemoryOntologyRuntimeStore()
    await valid_store.publish(
        _publication("tenant-a", "sales", "2" * 32, description="v2")
    )
    await valid_store.bind(
        PipelineOntologyBinding(
            tenant_id="tenant-a",
            pipeline_id="pipeline",
            block_id="block",
            ontology_id="sales",
            selector=OntologySliceSelector(resource_ids=("core.customer",)),
        )
    )
    valid_slice = await valid_store.load_runtime_slice(request)

    class WrongTenantStore:
        async def load_runtime_slice(
            self, requested: OntologySliceRequest
        ) -> OntologySlice:
            return valid_slice.model_copy(
                update={
                    "metadata": valid_slice.metadata.model_copy(
                        update={"tenant_id": "tenant-b"}
                    )
                }
            )

    with pytest.raises(OntologyCompatibilityError, match="another tenant"):
        await OntologyRuntimeManager(WrongTenantStore()).resolve(request)

    dangling = Ontology(
        version="vision-1",
        resources=(
            OntologyResource(
                resource_id="core.customer",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Customer",
                references=("core.missing",),
            ),
        ),
    )
    dangling_publication = _publication(
        "tenant-a", "dangling", "3" * 32, description="dangling"
    ).model_copy(
        update={
            "materialization": _materialization(
                "tenant-a", "3" * 32, description="dangling"
            ).model_copy(update={"ontology": dangling})
        }
    )
    dangling_store = InMemoryOntologyRuntimeStore()
    await dangling_store.publish(dangling_publication)
    await dangling_store.bind(
        PipelineOntologyBinding(
            tenant_id="tenant-a",
            pipeline_id="dangling",
            block_id="block",
            ontology_id="dangling",
            selector=OntologySliceSelector(resource_ids=("core.customer",)),
        )
    )
    dangling_slice = await dangling_store.load_runtime_slice(
        OntologySliceRequest(
            tenant_id="tenant-a", pipeline_id="dangling", block_id="block"
        )
    )
    assert dangling_slice.ontology.require("core.customer").references == (
        "core.missing",
    )
