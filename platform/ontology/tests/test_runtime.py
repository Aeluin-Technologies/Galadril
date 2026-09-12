"""Runtime tests for consuming Registry-derived ontology slices."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from galadril_ontology import (
    BlockOntologyContract,
    Ontology,
    OntologyResource,
    OntologyRuntimeManager,
    OntologySlice,
    OntologySliceMetadata,
    OntologySliceRequest,
    ResourceKind,
    active_ontology_slice,
)
from galadril_ontology.errors import (
    OntologyCompatibilityError,
    OntologyNotFoundError,
)
from pydantic import TypeAdapter, ValidationError


class StubRuntimeStore:
    """Returns pre-derived slices without reproducing Registry semantics."""

    __slots__ = ("_load_count", "_responses")

    def __init__(self, *responses: OntologySlice | Exception) -> None:
        self._responses = responses
        self._load_count = 0

    @property
    def load_count(self) -> int:
        return self._load_count

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice:
        del request
        position = self._load_count
        self._load_count += 1
        response = self._responses[position]
        if isinstance(response, Exception):
            raise response
        return response


def _slice(
    tenant_id: str = "tenant-a",
    revision_id: str = "1" * 32,
    *,
    include_property: bool = True,
) -> OntologySlice:
    resources = [
        OntologyResource(
            resource_id="core.customer",
            kind=ResourceKind.OBJECT_TYPE,
            display_name="Customer",
        )
    ]
    if include_property:
        resources.append(
            OntologyResource(
                resource_id="core.customer.email",
                kind=ResourceKind.PROPERTY,
                display_name="Email",
                owner_id="core.customer",
                value_type="string",
            )
        )
    return OntologySlice(
        metadata=OntologySliceMetadata(
            tenant_id=tenant_id,
            ontology_id="sales",
            publication_id="publication-1",
            revision_id=revision_id,
            base_version="vision-1",
            base_hash="a" * 64,
            effective_hash="b" * 64,
            publication_metadata={"environment": "production"},
            binding_metadata={"purpose": "identity"},
            published_at=datetime(2026, 8, 25, tzinfo=UTC),
        ),
        ontology=Ontology(version="vision-1", resources=tuple(resources)),
    )


def _request() -> OntologySliceRequest:
    return OntologySliceRequest(
        tenant_id="tenant-a",
        pipeline_id="customer-pipeline",
        pipeline_revision_id="0" * 32,
        block_id="resolve",
    )


def test_runtime_package_has_no_semantic_store_or_slice_implementation() -> (
    None
):
    """Keeps graph derivation exclusively behind the Registry boundary."""
    import galadril_ontology
    import galadril_ontology.runtime as runtime

    assert not hasattr(galadril_ontology, "InMemoryOntologyRuntimeStore")
    assert not hasattr(runtime, "_slice_ontology")


@pytest.mark.anyio
async def test_runtime_consumes_each_registry_slice_without_deriving_it() -> (
    None
):
    """Loads an immutable slice for every execution request."""
    first = _slice(revision_id="1" * 32)
    second = _slice(revision_id="2" * 32, include_property=False)
    store = StubRuntimeStore(first, second)
    manager = OntologyRuntimeManager(store)

    assert await manager.resolve(_request()) is first
    assert await manager.resolve(_request()) is second
    assert store.load_count == 2


@pytest.mark.anyio
async def test_runtime_validates_contract_and_binds_active_context() -> None:
    """Rejects incompatible slices and resets task-local state."""
    ontology_slice = _slice()
    manager = OntologyRuntimeManager(
        StubRuntimeStore(ontology_slice, ontology_slice, ontology_slice)
    )
    contract = BlockOntologyContract(
        required_resource_ids=("core.customer.email",),
        allowed_kinds=(ResourceKind.OBJECT_TYPE, ResourceKind.PROPERTY),
    )

    assert active_ontology_slice() is None
    async with manager.bind(_request(), contract) as bound:
        assert active_ontology_slice() is bound
    assert active_ontology_slice() is None

    with pytest.raises(OntologyCompatibilityError, match="required resource"):
        await manager.resolve(
            _request(),
            BlockOntologyContract(required_resource_ids=("core.missing",)),
        )
    with pytest.raises(OntologyCompatibilityError, match="resource kind"):
        await manager.resolve(
            _request(),
            BlockOntologyContract(allowed_kinds=(ResourceKind.EVENT_TYPE,)),
        )


def test_runtime_schema_serialization_and_request_validation() -> None:
    """Round-trips immutable Registry provenance and rejects unsafe IDs."""
    ontology_slice = _slice()

    assert (
        OntologySlice.model_validate_json(ontology_slice.model_dump_json())
        == ontology_slice
    )
    assert TypeAdapter(OntologySlice | None).validate_json("null") is None
    with pytest.raises(ValidationError):
        OntologySliceRequest(
            tenant_id="tenant-a",
            pipeline_id="bad id",
            pipeline_revision_id="0" * 32,
            block_id="block",
        )


@pytest.mark.anyio
async def test_runtime_rejects_wrong_tenant_and_propagates_absence() -> None:
    """Fails closed on corrupt provenance and unavailable Registry state."""
    manager = OntologyRuntimeManager(
        StubRuntimeStore(
            _slice(tenant_id="tenant-b"),
            OntologyNotFoundError("ontology unavailable"),
        )
    )

    with pytest.raises(OntologyCompatibilityError, match="another tenant"):
        await manager.resolve(_request())
    with pytest.raises(OntologyNotFoundError, match="unavailable"):
        await manager.resolve(_request())


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
