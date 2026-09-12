"""Vision integration tests for Registry-derived execution contexts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from galadril_ontology import (
    Ontology,
    OntologyNotFoundError,
    OntologyResource,
    OntologyRuntimeManager,
    OntologySlice,
    OntologySliceMetadata,
    OntologySliceRequest,
    ResourceKind,
    active_ontology_slice,
)
from galadril_pipeline.config import StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_vision.actors.processor import (
    CommandProcessingError,
    VisionCommandProcessor,
)
from galadril_vision.common.config import VisionConfig


class StubRegistryStore:
    """Returns a pre-derived Registry response without semantic fallback."""

    __slots__ = ("_response", "load_count")

    def __init__(self, response: OntologySlice | Exception) -> None:
        self._response = response
        self.load_count = 0

    async def load_runtime_slice(
        self, request: OntologySliceRequest
    ) -> OntologySlice:
        del request
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


def _registry_slice() -> OntologySlice:
    return OntologySlice(
        metadata=OntologySliceMetadata(
            tenant_id="tenant-a",
            ontology_id="operations",
            publication_id="2" * 32,
            revision_id="1" * 32,
            base_version="vision-1",
            base_hash="a" * 64,
            effective_hash="b" * 64,
            published_at=datetime(2026, 9, 9, tzinfo=UTC),
        ),
        ontology=Ontology(
            version="vision-1",
            resources=(
                OntologyResource(
                    resource_id="core.customer",
                    kind=ResourceKind.OBJECT_TYPE,
                    display_name="Customer",
                ),
            ),
        ),
    )


def _command() -> PipelineCommand:
    return PipelineCommand(
        correlation_id=uuid4(),
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
        _config(), ontology_runtime=OntologyRuntimeManager(store)
    )

    assert active_ontology_slice() is None
    with pytest.raises(CommandProcessingError, match="dedicated event-driven"):
        await processor.process(_command())
    assert active_ontology_slice() is None
    assert store.load_count == 1


@pytest.mark.anyio
async def test_processor_fails_closed_without_registry_binding() -> None:
    """Prevents execution when Registry cannot resolve the pinned binding."""
    store = StubRegistryStore(OntologyNotFoundError("ontology unavailable"))
    processor = VisionCommandProcessor(
        _config(), ontology_runtime=OntologyRuntimeManager(store)
    )

    with pytest.raises(CommandProcessingError, match="ontology unavailable"):
        await processor.process(_command())


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
