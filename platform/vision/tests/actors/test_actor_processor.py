"""Unit tests for actor-local command contract enforcement."""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import structlog
from galadril_pipeline.config import PipelineStep, StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_vision.actors.processor import (
    CommandProcessingError,
    VisionCommandProcessor,
)
from galadril_vision.adapters.builtin import (
    BuiltinStepAdapter,
    _require_tenant_storage_key,
)
from galadril_vision.application.execution import PipelineExecutor
from galadril_vision.common.config import VisionConfig
from pydantic import JsonValue


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


def test_native_routing_executes_with_an_empty_plugin_registry() -> None:
    from galadril_plugins import PluginRegistry

    config = _config()
    config.runtime_tenant_id = "tenant_a"
    data = config.model_dump()
    data["pipeline"] = [
        {
            "step": "classify",
            "type": "route",
            "input_from": ["source"],
            "routing": "regex",
            "branches": {"yes": ["sink"]},
            "params": {
                "field": "text",
                "pattern": "hello",
                "match_branch": "yes",
                "default_branch": "yes",
            },
        },
        {"step": "sink", "type": "sink", "input_from": ["classify"]},
    ]
    config = VisionConfig.model_validate(data)
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline=config.name,
        tenant_id="tenant_a",
        step="classify",
        step_type="route",
        resource_class="cpu",
        payload={"text": "hello"},
    )
    result = asyncio.run(
        VisionCommandProcessor(config, plugins=PluginRegistry(())).process(
            command
        )
    )
    assert result["branch"] == "yes"


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
    adapter = BuiltinStepAdapter(_config())
    identity_runtime = MagicMock()
    adapter._identity_state.runtime = identity_runtime
    processor = VisionCommandProcessor(
        _config(), handler_factory=lambda config: {"transform": adapter}
    )
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
    assert adapter._identity_state.runtime is identity_runtime
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
        current: PipelineExecutor, received: PipelineCommand
    ) -> dict[str, str]:
        assert current is processor._executor
        assert received is command
        return completed

    with (
        patch.object(
            PipelineExecutor,
            "process",
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
    _require_tenant_storage_key("tenant_a/raw/camera/frame.jpg", "tenant_a")


def test_processor_injects_route_handlers_after_admission() -> None:
    handler = MagicMock()
    handler.execute = AsyncMock(return_value={"backend": "custom"})
    factory = MagicMock(return_value={"transform": handler})
    config = _config()
    config.runtime_tenant_id = "tenant_a"
    processor = VisionCommandProcessor(config, handler_factory=factory)
    command = PipelineCommand(
        correlation_id=uuid4(),
        tenant_id="tenant_a",
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )
    assert asyncio.run(processor.process(command)) == {"backend": "custom"}
    with pytest.raises(CommandProcessingError):
        asyncio.run(
            processor.process(
                command.model_copy(update={"tenant_id": "tenant_b"})
            )
        )
    factory.assert_called_once()
    handler.execute.assert_awaited_once()


def test_builtin_resources_are_owned_by_each_route() -> None:
    first = _config()
    second = _config()
    second.name = "second"
    second.connectors.postgres.database = "second_database"
    second.connectors.s3.bucket = "second_raw"
    processor = VisionCommandProcessor((first, second))
    first.connectors.postgres.database = "mutated_after_composition"
    second.connectors.s3.bucket = "mutated_after_composition"
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="transform",
        step_type=StepType.DBT,
        resource_class=ResourceClass.CPU,
    )
    observed: list[tuple[str, object]] = []

    async def capture(
        adapter: object, received: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        observed.append((received.pipeline, adapter))
        return {}

    with patch(
        "galadril_vision.adapters.builtin.BuiltinStepAdapter.execute",
        new=capture,
    ):
        asyncio.run(processor.process(command))
        asyncio.run(
            processor.process(command.model_copy(update={"pipeline": "second"}))
        )
        asyncio.run(processor.process(command))
    assert observed[0][1] is observed[2][1]
    assert observed[0][1] is not observed[1][1]
    first_adapter = observed[0][1]
    second_adapter = observed[1][1]
    assert isinstance(first_adapter, BuiltinStepAdapter)
    assert isinstance(second_adapter, BuiltinStepAdapter)
    assert first_adapter._postgres_state is not second_adapter._postgres_state
    assert first_adapter._config.postgres.database == "test"
    assert second_adapter._config.postgres.database == "second_database"
    assert first_adapter._config.raw_store.bucket == "raw"
    assert second_adapter._config.raw_store.bucket == "second_raw"


def test_same_tenant_routes_share_identity_sequence_owner() -> None:
    first = _config()
    first.runtime_tenant_id = "tenant_a"
    second = first.model_copy(deep=True, update={"name": "second"})
    processor = VisionCommandProcessor((first, second))
    first_adapter = (
        processor._executor._routes[first.name].steps["transform"].handler
    )
    second_adapter = (
        processor._executor._routes[second.name].steps["transform"].handler
    )
    assert isinstance(first_adapter, BuiltinStepAdapter)
    assert isinstance(second_adapter, BuiltinStepAdapter)
    assert first_adapter._identity_state is second_adapter._identity_state


@pytest.mark.parametrize("conflict", ["identity", "database"])
def test_same_tenant_rejects_conflicting_identity_configuration(
    conflict: str,
) -> None:
    first = _config()
    first.runtime_tenant_id = "tenant_a"
    first.identity_resolution.enabled = True
    second = first.model_copy(deep=True, update={"name": "second"})
    if conflict == "identity":
        second.identity_resolution.candidate_top_k = 16
    else:
        second.connectors.postgres.database = "other_database"
    with pytest.raises(ValueError, match="identity"):
        VisionCommandProcessor((first, second))


def test_raw_storage_key_rejects_layer_first_layout() -> None:
    with pytest.raises(CommandProcessingError):
        _require_tenant_storage_key("raw/tenant_a/camera/frame.jpg", "tenant_a")

    with pytest.raises(CommandProcessingError, match="tenant partition"):
        _require_tenant_storage_key("tenant_b/raw/camera/frame.jpg", "tenant_a")
    with pytest.raises(CommandProcessingError, match="tenant partition"):
        _require_tenant_storage_key("raw/TENANT_A/camera/frame.jpg", "tenant_a")


def test_linked_plugin_scope_uses_trusted_block_origin_with_root_execution() -> (
    None
):
    config = _config()
    data = config.model_dump()
    data.update(
        runtime_tenant_id="tenant",
        runtime_pipeline_id="root",
        runtime_revision_id="a" * 32,
    )
    data["pipeline"] = [
        {
            "step": "linked_count",
            "type": "algorithm",
            "input_from": ["source"],
            "capability_id": "fixture.algorithm.count",
            "origin": {
                "pipeline_id": "child",
                "revision_id": "b" * 32,
                "block_id": "count",
            },
        }
    ]
    config = VisionConfig.model_validate(data)
    registry = MagicMock()
    VisionCommandProcessor(config, plugins=registry)
    scope = registry.handler.call_args.args[0]
    assert scope.tenant_id == "tenant"
    assert scope.pipeline_id == "child"
    assert scope.revision_id == "b" * 32
    assert scope.execution_id == config.name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
