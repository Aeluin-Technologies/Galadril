"""Unit tests targeting functional tracking and mapping bounds across Dagster pipelines."""

import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import dagster as dg
from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.s3.S3Client")
@patch("galadril_vision.connectors.s3.transit.S3TransitService.upload_batch")
async def test_asset_staged_batch_empty(
    mock_upload: MagicMock, mock_s3_cls: MagicMock
) -> None:
    """Ensures short-circuit evaluation paths handle unpopulated streaming windows gracefully."""
    from galadril_vision.pipeline.defs import staged_batch

    mock_context = MagicMock(spec=dg.AssetExecutionContext)
    mock_kafka = AsyncMock()
    mock_kafka.poll_batch.return_value = []

    mock_s3_res = MagicMock()

    result = await staged_batch(mock_context, mock_kafka, mock_s3_res)  # type: ignore

    assert isinstance(result, BatchHandle)
    assert result.payload == ""
    assert result.kafka_offsets == {}
    mock_upload.assert_not_called()


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.s3.S3Client")
@patch("galadril_vision.connectors.s3.transit.S3TransitService.upload_batch")
async def test_asset_staged_batch_populated(
    mock_upload: AsyncMock, mock_s3_cls: MagicMock
) -> None:
    """Validates structural metadata transformations and successful uploads for populated batches."""
    from galadril_vision.pipeline.defs import staged_batch

    mock_context = MagicMock(spec=dg.AssetExecutionContext)
    mock_kafka = AsyncMock()
    mock_kafka.poll_batch.return_value = [
        {"tenant_id": "tenant_123", "record_id": "rec_1"}
    ]
    mock_kafka.get_current_offsets.return_value = {"topic": {0: 500}}

    mock_s3_res = MagicMock()
    mock_upload.return_value = "s3://bucket/batches/tenant_123/mock.parquet"

    result = await staged_batch(mock_context, mock_kafka, mock_s3_res)  # type: ignore

    assert isinstance(result, BatchHandle)
    assert result.payload == "s3://bucket/batches/tenant_123/mock.parquet"
    assert result.kafka_offsets == {"topic": {0: 500}}

    mock_kafka.commit_offsets.assert_called_once_with({"topic": {0: 500}})
    mock_context.add_output_metadata.assert_called_once()


@pytest.mark.asyncio
async def test_asset_execute_pipeline_empty() -> None:
    """Ensures unpopulated pipeline references short-circuit out execution engines entirely."""
    from galadril_vision.pipeline.defs import execute_pipeline

    mock_context = MagicMock(spec=dg.AssetExecutionContext)
    mock_executor = AsyncMock()

    staged = BatchHandle(
        correlation_id="1", kafka_offsets={}, started_at=0.0, payload=""
    )

    result = await execute_pipeline(mock_context, staged, mock_executor)  # type: ignore

    assert result.payload.processed_records == 0
    mock_executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_asset_execute_pipeline_populated() -> None:
    """Validates distributed batch tracking transitions pass processing variables safely down to compute pools."""
    from galadril_vision.pipeline.defs import execute_pipeline

    mock_context = MagicMock(spec=dg.AssetExecutionContext)
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = PipelineResult(
        processed_records=250, duration=12.4
    )

    staged = BatchHandle(
        correlation_id="abc",
        kafka_offsets={"t": {0: 1}},
        started_at=100.0,
        payload="s3://target",
    )

    result = await execute_pipeline(mock_context, staged, mock_executor)  # type: ignore

    assert result.payload.processed_records == 250
    mock_executor.execute.assert_called_once_with("s3://target")
    mock_context.add_output_metadata.assert_called_once()


@pytest.mark.asyncio
async def test_asset_run_causal_active() -> None:
    """Validates that downstream assertion dependencies trigger reliably when parsing entries exist."""
    from galadril_vision.pipeline.defs import run_causal

    mock_context = MagicMock(spec=dg.AssetExecutionContext)
    mock_runner = AsyncMock()

    pipeline_res = PipelineResult(processed_records=10, duration=1.0)
    input_batch = BatchHandle(
        correlation_id="c",
        kafka_offsets={},
        started_at=0.0,
        payload=pipeline_res,
    )

    result = await run_causal(mock_context, input_batch, mock_runner)  # type: ignore

    assert result is input_batch
    mock_runner.run.assert_called_once_with(batch=input_batch)


@pytest.mark.asyncio
async def test_asset_run_causal_skipped() -> None:
    """Ensures downstream checking runs bypass tracking mechanics cleanly when processed sets are empty."""
    from galadril_vision.pipeline.defs import run_causal

    mock_context = MagicMock(spec=dg.AssetExecutionContext)
    mock_runner = AsyncMock()

    pipeline_res = PipelineResult(processed_records=0, duration=0.0)
    input_batch = BatchHandle(
        correlation_id="c",
        kafka_offsets={},
        started_at=0.0,
        payload=pipeline_res,
    )

    result = await run_causal(mock_context, input_batch, mock_runner)  # type: ignore

    assert result is input_batch
    mock_runner.run.assert_not_called()


def test_kafka_microbatch_sensor_trigger() -> None:
    """Validates sensor evaluation yields a RunRequest when streaming partition lag is discovered."""
    from galadril_vision.pipeline.defs import kafka_microbatch_sensor

    mock_context = MagicMock(spec=dg.SensorEvaluationContext)
    mock_kafka = MagicMock()
    mock_kafka.has_lag.return_value = True

    gen = kafka_microbatch_sensor(mock_context, mock_kafka)
    results = list(gen)  # type: ignore

    assert len(results) == 1
    assert isinstance(results[0], dg.RunRequest)


def test_kafka_microbatch_sensor_skip() -> None:
    """Validates sensor evaluation yields a SkipReason message when partition structures match current watermarks."""
    from galadril_vision.pipeline.defs import kafka_microbatch_sensor

    mock_context = MagicMock(spec=dg.SensorEvaluationContext)
    mock_kafka = MagicMock()
    mock_kafka.has_lag.return_value = False

    gen = kafka_microbatch_sensor(mock_context, mock_kafka)
    results = list(gen)  # type: ignore

    assert len(results) == 1
    assert isinstance(results[0], dg.SkipReason)


@pytest.mark.asyncio
async def test_pipeline_executor_resource_lifecycle() -> None:
    """Verifies that PipelineExecutorResource initializes environment contexts and forwards runs."""
    from galadril_vision.pipeline.defs import PipelineExecutorResource

    mock_init_context = MagicMock(spec=dg.InitResourceContext)

    mock_config_provider = MagicMock()
    mock_config_provider.vision_config.connectors.s3.access_key = "mock_key"
    mock_config_provider.vision_config.connectors.s3.secret_key = "mock_secret"
    mock_config_provider.vision_config.connectors.s3.region = "us-east-1"
    mock_config_provider.vision_config.connectors.s3.staging_bucket = (
        "mock_bucket"
    )
    mock_config_provider.vision_config.ray.address = "ray://mock:6379"
    mock_config_provider.pipeline_config = {"some": "config"}

    mock_db_provider = MagicMock()
    mock_db_provider.client = MagicMock()

    resource = PipelineExecutorResource(
        config_provider=mock_config_provider, db_provider=mock_db_provider
    )

    with pytest.raises(
        RuntimeError, match="PipelineExecutorResource accessed before setup."
    ):
        await resource.execute("s3://test-uri")

    with (
        patch("daft.set_runner_ray") as mock_set_ray,
        patch(
            "galadril_vision.pipeline.defs.ESKGPipelineExecutor"
        ) as mock_executor_cls,
    ):
        mock_executor_instance = AsyncMock()
        mock_executor_instance.execute.return_value = PipelineResult(
            processed_records=42, duration=1.5
        )
        mock_executor_cls.return_value = mock_executor_instance

        resource.setup_for_execution(mock_init_context)

        assert os.environ["AWS_ACCESS_KEY_ID"] == "mock_key"
        assert os.environ["AWS_SECRET_ACCESS_KEY"] == "mock_secret"
        assert os.environ["VISION_STAGING_BUCKET"] == "mock_bucket"

        mock_set_ray.assert_called_once_with(
            address="ray://mock:6379", noop_if_initialized=True
        )

        res = await resource.execute("s3://valid-uri")
        assert res.processed_records == 42
        mock_executor_instance.execute.assert_called_once_with("s3://valid-uri")
