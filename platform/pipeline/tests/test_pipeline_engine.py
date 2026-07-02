"""Integration and behavior tests targeting the concurrent AsyncPipelineEngine architecture."""

import asyncio
from datetime import datetime, timezone
from typing import Any
import pytest

from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.runtime.batch import BatchHandle
from galadril_pipeline.runtime.engine import (
    AbstractCheckpointStore,
    AsyncPipelineEngine,
)
from galadril_pipeline.runtime.schemas import (
    AbstractStepExecutor,
    NodeStatus,
    PipelineRunContext,
    StepCheckpoint,
    StepRuntimeInput,
    StepRuntimeOutput,
)


class MockStepExecutor(AbstractStepExecutor):
    """In-memory executor stub simulating transactional processing mutation chains."""

    def __init__(self) -> None:
        super().__init__()
        self.executed_steps: list[str] = []
        self.should_fail: bool = False

    async def execute_step(
        self, runtime_input: StepRuntimeInput[list[dict[str, Any]]]
    ) -> StepRuntimeOutput[list[dict[str, Any]]]:
        self.executed_steps.append(runtime_input.step_name)
        await asyncio.sleep(0.001)

        if self.should_fail:
            return StepRuntimeOutput(
                status=NodeStatus.FAILED,
                batch=BatchHandle(
                    correlation_id=runtime_input.correlation_id,
                    payload=[],
                    kafka_offsets={},
                ),
                error_details="Simulated backend compute matrix failure.",
            )

        return StepRuntimeOutput(
            status=NodeStatus.COMPLETED,
            batch=BatchHandle(
                correlation_id=runtime_input.correlation_id,
                payload=[{"mutated": True}],
                kafka_offsets={},
            ),
            records_processed=1,
            latency_seconds=0.01,
            storage_uri_pointers=[
                f"s3://bucket/output/{runtime_input.step_name}.parquet"
            ],
            metrics={"accuracy": 0.95},
        )


class MockCheckpointStore(AbstractCheckpointStore):
    """InMemory checkpoint tracker storing immutable operational records."""

    def __init__(self) -> None:
        self.checkpoints: dict[tuple[str, str], StepCheckpoint] = {}

    async def get_checkpoint(
        self, run_id: str, step_name: str
    ) -> StepCheckpoint | None:
        return self.checkpoints.get((run_id, step_name))

    async def save_checkpoint(
        self, run_id: str, checkpoint: StepCheckpoint
    ) -> None:
        self.checkpoints[(run_id, checkpoint.step_name)] = checkpoint


@pytest.fixture
def valid_pipeline_payload() -> dict[str, Any]:
    """Returns a valid operational dictionary structure mapping the pipeline configuration."""
    return {
        "version": 1,
        "name": "production_vision_pipeline",
        "sources": [
            {
                "id": "ingress_images",
                "topic": "raw-images",
                "match_pattern": r"^.*\.jpg$",
                "schema_path": "/schemas/image.avsc",
            }
        ],
        "pipeline": [
            {
                "step": "image_inference",
                "type": "inference",
                "input_from": ["ingress_images"],
                "model": "galadril.vision.models.ResNet50",
                "artifact_path": "/models/resnet50.bin",
                "params": {
                    "trigger": "manual",
                    "retry_policy": {"max_retries": 1, "delay_seconds": 0.1},
                },
            },
            {
                "step": "data_sink",
                "type": "sink",
                "input_from": ["image_inference"],
                "params": {"trigger": "cron", "cron": "0 12 * * *"},
            },
        ],
    }


@pytest.fixture
def run_context() -> PipelineRunContext:
    """Provides a unified run validation context identity token."""
    return PipelineRunContext(
        run_id="run_01j00000000000000000000001",
        correlation_id="trace_01j00000000000000000000002",
        tenant_id="tenant_global",
    )


@pytest.mark.asyncio
async def test_async_pipeline_engine_execution(
    valid_pipeline_payload: dict[str, Any], run_context: PipelineRunContext
) -> None:
    """Executes the full runtime pipeline to verify transactional idempotence and state calculation."""
    config = PipelineConfig.model_validate(valid_pipeline_payload)

    executor = MockStepExecutor()
    store = MockCheckpointStore()
    engine = AsyncPipelineEngine(
        executor=executor, checkpoint_store=store, max_concurrent_tasks=2
    )

    results = await engine.execute_pipeline(config, run_context)

    assert "image_inference" in results
    assert "data_sink" in results
    assert results["image_inference"].status == NodeStatus.COMPLETED
    assert results["data_sink"].status == NodeStatus.COMPLETED
    assert executor.executed_steps == ["image_inference", "data_sink"]

    # Verify cryptographic signature verification inside the store.
    checkpoint = await store.get_checkpoint(
        run_context.run_id, "image_inference"
    )
    assert checkpoint is not None
    assert len(checkpoint.payload_checksum) == 64


@pytest.mark.asyncio
async def test_async_pipeline_engine_checkpoint_bypass(
    valid_pipeline_payload: dict[str, Any], run_context: PipelineRunContext
) -> None:
    """Guarantees that pre-existing completed checkpoints bypass node computational execution blocks."""
    config = PipelineConfig.model_validate(valid_pipeline_payload)

    executor = MockStepExecutor()
    store = MockCheckpointStore()
    engine = AsyncPipelineEngine(
        executor=executor, checkpoint_store=store, max_concurrent_tasks=2
    )

    # Pre-populate checkpoint store with successful execution receipt.
    pre_existing_checkpoint = StepCheckpoint(
        step_name="image_inference",
        correlation_id=run_context.correlation_id,
        status=NodeStatus.COMPLETED,
        completed_at=datetime.now(timezone.utc),
        records_processed=42,
        storage_uri_pointers=["s3://cached-path"],
        payload_checksum="a" * 64,
    )
    await store.save_checkpoint(run_context.run_id, pre_existing_checkpoint)

    results = await engine.execute_pipeline(config, run_context)

    assert results["image_inference"].status == NodeStatus.COMPLETED
    assert results["data_sink"].status == NodeStatus.COMPLETED
    assert "image_inference" not in executor.executed_steps
    assert "data_sink" in executor.executed_steps


@pytest.mark.asyncio
async def test_async_pipeline_engine_upstream_failure_propagation(
    valid_pipeline_payload: dict[str, Any], run_context: PipelineRunContext
) -> None:
    """Verifies that an upstream node failure down-grades downstream nodes to SKIPPED statuses."""
    config = PipelineConfig.model_validate(valid_pipeline_payload)

    executor = MockStepExecutor()
    executor.should_fail = True
    store = MockCheckpointStore()
    engine = AsyncPipelineEngine(
        executor=executor, checkpoint_store=store, max_concurrent_tasks=2
    )

    results = await engine.execute_pipeline(config, run_context)

    assert results["image_inference"].status == NodeStatus.FAILED
    assert results["data_sink"].status == NodeStatus.SKIPPED
    assert "data_sink" not in executor.executed_steps
