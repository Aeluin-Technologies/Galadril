"""Vision integration tests for Registry-derived execution contexts."""

from __future__ import annotations

from uuid import uuid4

import pytest
from galadril_ontology import (
    GolDenied,
    GolLease,
    GolRequest,
    GolRuntimeManager,
    GolSymbolKind,
    active_gol_lease,
)
from galadril_pipeline.config import StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_vision.actors.processor import (
    CommandProcessingError,
    VisionCommandProcessor,
)
from galadril_vision.common.config import VisionConfig
from galadril_vision.common.eskg import (
    GraphNodeKind,
    resolve_gol_reference,
)


class StubRegistryStore:
    """Returns a pre-derived Registry response without semantic fallback."""

    __slots__ = ("_response", "load_count")

    def __init__(self, response: GolLease | Exception) -> None:
        self._response = response
        self.load_count = 0

    async def open(self, request: GolRequest, admission: str) -> GolLease:
        del request, admission
        self.load_count += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _config() -> VisionConfig:
    return VisionConfig.model_validate(
        {
            "name": "vision",
            "connectors": {
                "kafka": {
                    "brokers": ["localhost:9092"],
                    "schema_registry": "http://localhost:8081",
                    "consumer_group": "test",
                },
                "s3": {
                    "endpoint": "http://localhost:9000",
                    "access_key": "test",
                    "secret_key": "test",
                    "region": "us-east-1",
                    "bucket": "raw",
                },
                "postgres": {
                    "database": "test",
                    "host": "localhost:5432",
                    "user": "test",
                    "password": "test",
                },
                "spicedb": {
                    "endpoint": "localhost:50051",
                    "token": "test",
                },
            },
            "pipeline": [
                {
                    "step": "transform",
                    "type": "dbt",
                    "input_from": ["source"],
                }
            ],
            "sources": [
                {
                    "id": "source",
                    "topic": "raw",
                    "match_pattern": ".*",
                    "schema_path": "schema.avsc",
                }
            ],
        }
    )


def _registry_slice() -> GolLease:
    return GolLease.model_validate(
        {
            "tenantId": "tenant-a",
            "ontologyId": "core",
            "ontologyVersion": "1.0.0",
            "sourceRevision": "1" * 32,
            "generationId": "2" * 32,
            "contextId": "server-context",
            "symbols": [
                {
                    "key": "core::Customer",
                    "name": "Customer",
                    "version": "1.0.0",
                    "kind": "entity",
                }
            ],
        }
    )


@pytest.mark.anyio
async def test_graph_classification_pins_active_registry_revision() -> None:
    manager = GolRuntimeManager(StubRegistryStore(_registry_slice()))
    request = GolRequest(
        source_object_id="raw:tenant-a/source/object",
        tenant_id="tenant-a",
        pipeline_id="vision",
        pipeline_revision_id="pipeline-revision",
        block_id="sink",
    )
    async with manager.bind(request, admission="server-admission"):
        reference = resolve_gol_reference(
            "core::Customer", "tenant-a", GraphNodeKind.ENTITY
        )
        assert reference is not None
        assert reference.source_revision == "1" * 32
        assert reference.ontology_id == "core"
        assert reference.symbol_kind == GolSymbolKind.ENTITY
        with pytest.raises(ValueError, match="kind"):
            resolve_gol_reference(
                "core::Customer", "tenant-a", GraphNodeKind.STATE
            )
        with pytest.raises(ValueError, match="tenant"):
            resolve_gol_reference(
                "core::Customer", "tenant-b", GraphNodeKind.ENTITY
            )
        with pytest.raises(ValueError, match="unavailable"):
            resolve_gol_reference(
                "core::Missing", "tenant-a", GraphNodeKind.ENTITY
            )
    with pytest.raises(ValueError, match="GOL calculation"):
        resolve_gol_reference(
            "core::Customer", "tenant-a", GraphNodeKind.ENTITY
        )
    assert resolve_gol_reference(None, "tenant-a", GraphNodeKind.ENTITY) is None


def _command() -> PipelineCommand:
    return PipelineCommand(
        correlation_id=uuid4(),
        admission="server-admission",
        payload={
            "record": {"storage_path": "s3://lake/tenant-a/source/object"}
        },
        tenant_id="tenant-a",
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )


@pytest.mark.anyio
async def test_processor_binds_registry_runtime_slice() -> None:
    """Binds the Registry response before dispatch and always resets it."""
    store = StubRegistryStore(_registry_slice())
    processor = VisionCommandProcessor(
        _config(), ontology_runtime=GolRuntimeManager(store)
    )

    assert active_gol_lease() is None
    with pytest.raises(CommandProcessingError, match="dedicated event-driven"):
        await processor.process(_command())
    assert active_gol_lease() is None
    assert store.load_count == 1


@pytest.mark.anyio
async def test_processor_fails_closed_without_registry_binding() -> None:
    """Prevents execution when Registry cannot resolve the pinned binding."""
    store = StubRegistryStore(GolDenied("ontology unavailable"))
    processor = VisionCommandProcessor(
        _config(), ontology_runtime=GolRuntimeManager(store)
    )

    with pytest.raises(
        CommandProcessingError, match="authorization is unavailable"
    ):
        await processor.process(_command())


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
