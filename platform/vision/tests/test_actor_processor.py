"""Unit tests for actor-local command contract enforcement."""

from uuid import uuid4

import pytest
from galadril_pipeline.config import StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_vision.actors.processor import (
    CommandProcessingError,
    VisionCommandProcessor,
)
from galadril_vision.common.config import VisionConfig


def _config() -> VisionConfig:
    """Creates a minimal DBT pipeline without external runtime calls."""
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


@pytest.mark.asyncio
async def test_processor_rejects_unconfigured_runtime_adapter() -> None:
    """Fails DBT commands explicitly instead of silently skipping work."""
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )

    with pytest.raises(CommandProcessingError, match="dedicated event-driven"):
        await VisionCommandProcessor(_config()).process(command)


@pytest.mark.asyncio
async def test_processor_rejects_pipeline_identity_mismatch() -> None:
    """Prevents a shared actor from executing commands for another pipeline."""
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="other",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )

    with pytest.raises(CommandProcessingError, match="does not match"):
        await VisionCommandProcessor(_config()).process(command)
