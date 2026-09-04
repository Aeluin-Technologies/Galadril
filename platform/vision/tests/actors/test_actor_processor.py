"""Unit tests for actor-local command contract enforcement."""

import asyncio
import sys
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import structlog
from galadril_pipeline.config import StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_vision.actors.processor import (
    CommandProcessingError,
    VisionCommandProcessor,
    _require_tenant_storage_key,
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


def test_processor_rejects_unconfigured_runtime_adapter() -> None:
    """Fails DBT commands explicitly instead of silently skipping work."""
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )

    with pytest.raises(CommandProcessingError, match="dedicated event-driven"):
        asyncio.run(VisionCommandProcessor(_config()).process(command))


def test_processor_rejects_pipeline_identity_mismatch() -> None:
    """Prevents a shared actor from executing commands for another pipeline."""
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="other",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )

    with pytest.raises(CommandProcessingError, match="does not match"):
        asyncio.run(VisionCommandProcessor(_config()).process(command))


def test_shared_processor_selects_pipeline_by_tenant_and_revision() -> None:
    """One Ray actor can safely execute commands from several tenant DAGs."""
    tenant_a = _config()
    tenant_a.name = "tenant_a/daily/aaaaaaaa"
    tenant_a.runtime_tenant_id = "tenant_a"
    tenant_a.runtime_pipeline_id = "daily"
    tenant_b = _config()
    tenant_b.name = "tenant_b/daily/bbbbbbbb"
    tenant_b.runtime_tenant_id = "tenant_b"
    tenant_b.runtime_pipeline_id = "daily"
    processor = VisionCommandProcessor((tenant_a, tenant_b))
    valid = PipelineCommand(
        correlation_id=uuid4(),
        tenant_id="tenant_b",
        pipeline=tenant_b.name,
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )
    crossed = valid.model_copy(update={"tenant_id": "tenant_a"})

    with pytest.raises(CommandProcessingError, match="dedicated event-driven"):
        asyncio.run(processor.process(valid))
    with pytest.raises(CommandProcessingError, match="tenant or revision"):
        asyncio.run(processor.process(crossed))


def test_processor_sanitizes_failed_gpu_command_without_closing_licorne() -> (
    None
):
    """Vision cleanup preserves LI-ESKG's isolated multi-tenant runtime."""
    processor = VisionCommandProcessor(_config())
    identity_runtime = MagicMock()
    processor._identity_runtime = identity_runtime
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.GPU,
    )
    torch = MagicMock()

    async def scenario() -> None:
        structlog.contextvars.bind_contextvars(tenant_id="tenant_a")
        with pytest.raises(
            CommandProcessingError, match="dedicated event-driven"
        ):
            await processor.process(command)
        assert structlog.contextvars.get_contextvars() == {}

    with (
        patch("galadril_vision.actors.processor.gc.collect") as collect,
        patch.dict(sys.modules, {"torch": torch}),
    ):
        asyncio.run(scenario())

    identity_runtime.close.assert_not_called()
    assert processor._identity_runtime is identity_runtime
    collect.assert_called_once_with()
    torch.cuda.empty_cache.assert_called_once_with()


def test_processor_sanitizes_successful_cpu_command() -> None:
    """Runs mandatory cleanup on successful commands without touching CUDA."""
    processor = VisionCommandProcessor(_config())
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )
    completed = {"status": "complete"}
    torch = MagicMock()

    async def complete(
        current: VisionCommandProcessor, received: PipelineCommand
    ) -> dict[str, str]:
        assert current is processor
        assert received is command
        return completed

    with (
        patch.object(
            VisionCommandProcessor,
            "_process_isolated",
            new=complete,
        ),
        patch("galadril_vision.actors.processor.gc.collect") as collect,
        patch.dict(sys.modules, {"torch": torch}),
    ):
        result = asyncio.run(processor.process(command))

    assert result == completed
    collect.assert_called_once_with()
    torch.cuda.empty_cache.assert_not_called()


def test_storage_keys_are_partitioned_by_exact_tenant() -> None:
    """Rejects absolute and prefixed paths owned by another tenant."""
    _require_tenant_storage_key("tenant_a/camera/frame.jpg", "tenant_a")
    _require_tenant_storage_key("raw/tenant_a/camera/frame.jpg", "tenant_a")

    with pytest.raises(CommandProcessingError, match="tenant partition"):
        _require_tenant_storage_key("tenant_b/camera/frame.jpg", "tenant_a")
    with pytest.raises(CommandProcessingError, match="tenant partition"):
        _require_tenant_storage_key("raw/TENANT_A/camera/frame.jpg", "tenant_a")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
