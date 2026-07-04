"""Unit tests targeting functional tracking and mapping bounds across Dagster pipelines."""

import sys
from unittest.mock import MagicMock

mock_dagster_mod = MagicMock()
sys.modules["dagster"] = mock_dagster_mod

import pytest
from unittest.mock import AsyncMock, patch
from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult
from galadril_vision.connectors.kafka.validator import CanonicalRecord


@pytest.mark.asyncio
@patch("galadril_vision.pipeline.assets.validate_and_normalize_kafka_batch")
async def test_kafka_source_asset_ingestion(mock_validate: MagicMock) -> None:
    """Validates message conversion and metadata aggregation inside the ingestion asset."""
    from galadril_vision.pipeline.defs import kafka_source

    mock_context = MagicMock()
    mock_kafka_res = AsyncMock()
    mock_kafka_res.poll_batch.return_value = [
        {"topic": "vision", "payload": {}, "event_type": "TEST"}
    ]

    mock_batch = MagicMock()
    mock_batch.accepted = [MagicMock(spec=CanonicalRecord)]
    mock_batch.rejected = []
    mock_validate.return_value = mock_batch

    # type: ignore est requis ici car le décorateur Dagster masque la nature awaitable pour le linter
    result = await kafka_source(mock_context, mock_kafka_res)  # type: ignore

    assert isinstance(result, BatchHandle)
    assert len(result.payload) == 1
    mock_context.add_output_metadata.assert_called_once()


@pytest.mark.asyncio
async def test_stage_batch_asset_empty_payload() -> None:
    """Ensures short-circuit evaluation paths handle unpopulated stream sequences cleanly."""
    from galadril_vision.pipeline.defs import stage_batch

    mock_context = MagicMock()
    mock_transit = AsyncMock()

    empty_source = BatchHandle(
        correlation_id="xyz", kafka_offsets={}, payload=[]
    )

    result = await stage_batch(mock_context, empty_source, mock_transit)  # type: ignore
    assert result.payload == ""
    mock_transit.upload.assert_not_called()


@pytest.mark.asyncio
async def test_execute_pipeline_asset_materialization() -> None:
    """Ensures remote references pass processing parameters downstream correctly."""
    from galadril_vision.pipeline.defs import execute_pipeline

    mock_context = MagicMock()
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = PipelineResult(
        processed_records=42, duration=0.5
    )

    staged_batch = BatchHandle(
        correlation_id="abc", kafka_offsets={}, payload="s3://path.parquet"
    )

    result = await execute_pipeline(mock_context, staged_batch, mock_executor)  # type: ignore
    assert result.payload.processed_records == 42
    mock_executor.execute.assert_called_once_with("s3://path.parquet")
