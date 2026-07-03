"""Unit tests targeting concurrent orchestration and state replay loops."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from galadril_pipeline.config import (
    PipelineConfig,
    PipelineStep,
    Source,
    StepParams,
    StepType,
    RetryPolicy,
)
from galadril_pipeline.runtime.batch import BatchHandle
from galadril_pipeline.runtime.engine import (
    AsyncPipelineEngine,
    AbstractCheckpointStore,
)
from galadril_pipeline.runtime.schemas import (
    NodeStatus,
    PipelineRunContext,
    StepRuntimeOutput,
    StepCheckpoint,
)


class MockCheckpointStore(AbstractCheckpointStore):
    """In-memory checkpoint tracker stub."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], StepCheckpoint] = {}

    async def get_checkpoint(
        self, run_id: str, step_name: str
    ) -> StepCheckpoint | None:
        return self.store.get((run_id, step_name))

    async def save_checkpoint(
        self, run_id: str, checkpoint: StepCheckpoint
    ) -> None:
        self.store[(run_id, checkpoint.step_name)] = checkpoint


@pytest.mark.asyncio
async def test_engine_state_checksum_calculation() -> None:
    """Validates deterministic SHA-256 state hashing mechanics."""
    checksum = AsyncPipelineEngine._compute_state_checksum(
        "step", "run1", NodeStatus.COMPLETED, 10, ["s3://uri"], {"p": 1}
    )
    assert len(checksum) == 64


@pytest.mark.asyncio
async def test_pipeline_engine_execution_lifecycle() -> None:
    """Verifies successful concurrent step synchronization routing layouts."""
    source = Source(
        id="src", topic="topic", match_pattern=".*", schema_path="/p"
    )
    step = PipelineStep(step="proc", type=StepType.SINK, input_from=["src"])
    config = PipelineConfig(name="p", sources=[source], pipeline=[step])

    ctx = PipelineRunContext(run_id="run1", correlation_id="c1", tenant_id="t1")

    mock_executor = MagicMock()
    mock_output = StepRuntimeOutput(
        status=NodeStatus.COMPLETED,
        batch=BatchHandle(correlation_id="c1", payload="out"),
        records_processed=5,
    )
    mock_executor.execute_step = AsyncMock(return_value=mock_output)
    store = MockCheckpointStore()

    engine = AsyncPipelineEngine(executor=mock_executor, checkpoint_store=store)
    results = await engine.execute_pipeline(config, ctx)

    assert "proc" in results
    assert results["proc"].status == NodeStatus.COMPLETED
    assert results["proc"].records_mutated == 5


@pytest.mark.asyncio
async def test_pipeline_engine_upstream_failure_handling() -> None:
    """Ensures degraded upstream states automatically downgrade downstream steps to SKIPPED."""
    source = Source(
        id="src", topic="topic", match_pattern=".*", schema_path="/p"
    )
    step1 = PipelineStep(step="step1", type=StepType.SINK, input_from=["src"])
    step2 = PipelineStep(step="step2", type=StepType.SINK, input_from=["step1"])
    config = PipelineConfig(name="p", sources=[source], pipeline=[step1, step2])

    ctx = PipelineRunContext(run_id="run1", correlation_id="c1", tenant_id="t1")

    mock_executor = MagicMock()
    # Force step1 execution failure.
    mock_output = StepRuntimeOutput(
        status=NodeStatus.FAILED,
        batch=BatchHandle(correlation_id="c1", payload=""),
        error_details="Crash",
    )
    mock_executor.execute_step = AsyncMock(return_value=mock_output)
    store = MockCheckpointStore()

    engine = AsyncPipelineEngine(executor=mock_executor, checkpoint_store=store)
    results = await engine.execute_pipeline(config, ctx)

    assert results["step1"].status == NodeStatus.FAILED
    assert results["step2"].status == NodeStatus.SKIPPED


@pytest.mark.asyncio
async def test_engine_retry_policy_exhaustion() -> None:
    """Validates retry management loops when structural executions fail consistently."""
    step = PipelineStep(
        step="retry_step",
        type=StepType.SINK,
        input_from=[],
        params=StepParams(
            retry_policy=RetryPolicy(max_retries=1, delay_seconds=0.01)
        ),
    )
    ctx = PipelineRunContext(run_id="run1", correlation_id="c1", tenant_id="t1")
    batch = BatchHandle(correlation_id="c1", payload=[])

    mock_executor = MagicMock()
    mock_output = StepRuntimeOutput(
        status=NodeStatus.FAILED,
        batch=BatchHandle(correlation_id="c1", payload=""),
        error_details="Persistent error",
    )
    mock_executor.execute_step = AsyncMock(return_value=mock_output)
    store = MockCheckpointStore()

    engine = AsyncPipelineEngine(executor=mock_executor, checkpoint_store=store)
    checkpoint, out_batch = await engine._execute_with_retry_policy(
        step, ctx, [], batch
    )

    assert checkpoint.status == NodeStatus.FAILED
    assert out_batch is None
    # 1 initial attempt + 1 retry = 2 calls total.
    assert mock_executor.execute_step.call_count == 2
