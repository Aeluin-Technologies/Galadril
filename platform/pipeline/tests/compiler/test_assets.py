"""Unit tests checking Dagster asset generation and payload size calculation mechanics."""

from unittest.mock import AsyncMock, MagicMock

import dagster as dg
import pytest
from galadril_pipeline.compiler.assets import (
    AssetCompilerFactory,
    _get_payload_size,
)
from galadril_pipeline.config import (
    PipelineStep,
    RetryPolicy,
    Source,
    StepParams,
    StepType,
)
from galadril_pipeline.runtime.batch import BatchHandle
from galadril_pipeline.runtime.schemas import NodeStatus


def test_get_payload_size_branches() -> None:
    """Covers every condition inside the payload estimation helper utility."""
    assert _get_payload_size(None) == 0

    mock_custom = MagicMock()
    mock_custom.processed_records = 42
    assert _get_payload_size(mock_custom) == 42

    assert _get_payload_size("hello") == 1
    assert _get_payload_size("") == 0
    assert _get_payload_size([1, 2, 3]) == 3
    assert _get_payload_size(123) == 1


@pytest.mark.asyncio
async def test_build_source_asset_execution() -> None:
    """Validates compiled Kafka source asset execution when data is empty or full."""
    source_cfg = Source(
        id="test_src", topic="topic", match_pattern=".*", schema_path="/p"
    )
    asset_def = AssetCompilerFactory.build_source_asset(source_cfg)

    mock_kafka = MagicMock()
    mock_kafka.poll_batch = AsyncMock(return_value=[])

    res_empty = dg.materialize(
        assets=[asset_def], resources={"kafka": mock_kafka}
    )
    assert res_empty.success

    mock_kafka.poll_batch = AsyncMock(return_value=[{"record_id": "1"}])
    res_full = dg.materialize(
        assets=[asset_def], resources={"kafka": mock_kafka}
    )
    assert res_full.success


@pytest.mark.asyncio
async def test_build_pipeline_asset_execution() -> None:
    """Validates the selection, checking, and error paths of compiled pipeline steps."""
    step_cfg = PipelineStep(
        step="test_step",
        type=StepType.SINK,
        input_from=["upstream_node"],
        params=StepParams(
            retry_policy=RetryPolicy(max_retries=2, delay_seconds=0.1)
        ),
    )
    asset_def = AssetCompilerFactory.build_pipeline_asset(
        step_cfg, topological_index=1
    )

    mock_executor = MagicMock()

    mock_output = MagicMock()
    mock_output.status = NodeStatus.COMPLETED
    mock_output.records_processed = 5
    mock_output.latency_seconds = 0.5
    mock_output.batch = BatchHandle(correlation_id="c1", payload="done")
    mock_executor.execute_step = AsyncMock(return_value=mock_output)

    upstream_handle = BatchHandle(correlation_id="c1", payload=["data"])

    @dg.asset(name="upstream_node")
    def upstream_node_mock() -> BatchHandle:
        return upstream_handle

    res = dg.materialize(
        assets=[upstream_node_mock, asset_def],
        resources={"pipeline_executor": mock_executor},
    )
    assert res.success

    mock_output.status = NodeStatus.FAILED
    mock_output.error_details = "Execution matrix failed"

    with pytest.raises(RuntimeError, match="Step failed inside backend"):
        dg.materialize(
            assets=[upstream_node_mock, asset_def],
            resources={"pipeline_executor": mock_executor},
        )
