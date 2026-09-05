"""Vision pipeline integration tests for tenant ontology execution contexts."""

from __future__ import annotations

from uuid import uuid4

import pytest
from galadril_ontology import (
    InMemoryOntologyRuntimeStore,
    MaterializedOntology,
    Ontology,
    OntologyResource,
    OntologyRuntimeManager,
    OntologySliceSelector,
    OverlaySnapshot,
    PipelineOntologyBinding,
    PublishedOntology,
    ResourceKind,
    active_ontology_slice,
)
from galadril_ontology.model import ontology_content_hash
from galadril_pipeline.config import StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_vision.actors.processor import (
    CommandProcessingError,
    VisionCommandProcessor,
)
from galadril_vision.common.config import VisionConfig


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


@pytest.mark.anyio
async def test_processor_resolves_and_binds_postgres_runtime_slice() -> None:
    """Binds a block-specific ontology before dispatch and always resets it."""
    ontology = Ontology(
        version="vision-1",
        resources=(
            OntologyResource(
                resource_id="core.customer",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Customer",
            ),
        ),
    )
    materialization = MaterializedOntology(
        tenant_id="tenant-a",
        revision_id="1" * 32,
        base_version="vision-1",
        base_hash="a" * 64,
        effective_hash=ontology_content_hash(ontology),
        overlay=OverlaySnapshot(),
        ontology=ontology,
    )
    store = InMemoryOntologyRuntimeStore()
    await store.publish(
        PublishedOntology(
            tenant_id="tenant-a",
            ontology_id="operations",
            publication_id="2" * 32,
            materialization=materialization,
        )
    )
    await store.bind(
        PipelineOntologyBinding(
            tenant_id="tenant-a",
            pipeline_id="vision",
            block_id="transform",
            ontology_id="operations",
            selector=OntologySliceSelector(resource_ids=("core.customer",)),
        )
    )
    processor = VisionCommandProcessor(
        _config(), ontology_runtime=OntologyRuntimeManager(store)
    )
    command = PipelineCommand(
        correlation_id=uuid4(),
        tenant_id="tenant-a",
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )

    assert active_ontology_slice() is None
    with pytest.raises(CommandProcessingError, match="dedicated event-driven"):
        await processor.process(command)
    assert active_ontology_slice() is None
    assert store.load_count == 1


@pytest.mark.anyio
async def test_processor_fails_closed_without_pipeline_ontology_binding() -> (
    None
):
    """Prevents a block from running with another tenant or pipeline ontology."""
    processor = VisionCommandProcessor(
        _config(),
        ontology_runtime=OntologyRuntimeManager(InMemoryOntologyRuntimeStore()),
    )
    command = PipelineCommand(
        correlation_id=uuid4(),
        tenant_id="tenant-a",
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )

    with pytest.raises(CommandProcessingError, match="ontology unavailable"):
        await processor.process(command)


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
