"""Unit tests targeting the decoupled streaming pipeline orchestrator and routing loops."""

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from galadril_vision.connectors.kafka.consumer import IngestedMessage
from galadril_vision.pipeline.router import PipelineRouteKey
from galadril_vision.pipeline.runner import VisionPipeline


@pytest.mark.asyncio
@patch("galadril_vision.pipeline.runner.validate_and_normalize_kafka_batch")
async def test_vision_pipeline_batch_processing_routing(
    mock_validate: MagicMock,
) -> None:
    """Verifies message partitioning by tenant/topic and subsequent transit upload sequencing."""
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_router = AsyncMock()
    mock_dlq_producer = AsyncMock()

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
        pipeline_router=mock_router,
        dlq_producer=mock_dlq_producer,
        dlq_topic="authz-dlq",
    )

    mock_record = MagicMock()
    mock_record.model_dump.return_value = {
        "tenant_id": "tenant-a",
        "topic": "vision-frames",
        "payload": {},
    }

    mock_validated = MagicMock()
    mock_validated.accepted = [mock_record]
    mock_validated.rejected = []
    mock_validate.return_value = mock_validated

    pipeline._dispatch_sub_batch = AsyncMock(return_value=True)

    input_batch = [
        IngestedMessage(topic="vision-frames", payload={}, event_type="FRAME")
    ]
    success = await pipeline.process_batch(input_batch)

    assert success is True
    pipeline._dispatch_sub_batch.assert_called_once_with(
        PipelineRouteKey(tenant_id="tenant-a", topic="vision-frames"),
        [{"tenant_id": "tenant-a", "topic": "vision-frames", "payload": {}}],
    )


@pytest.mark.asyncio
@patch("galadril_vision.pipeline.runner.validate_and_normalize_kafka_batch")
async def test_vision_pipeline_rejected_records_sent_to_dlq(
    mock_validate: MagicMock,
) -> None:
    """Guarantees that unparseable or rejected records are isolated and routed to the DLQ."""
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_router = AsyncMock()
    mock_dlq_producer = AsyncMock()

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
        pipeline_router=mock_router,
        dlq_producer=mock_dlq_producer,
        dlq_topic="authz-dlq",
    )

    mock_validated = MagicMock()
    mock_validated.accepted = []
    mock_validated.rejected = ["corrupted_payload_string"]
    mock_validate.return_value = mock_validated

    success = await pipeline.process_batch([])

    assert success is True
    mock_dlq_producer.produce_json.assert_called_once_with(
        topic="authz-dlq",
        key="rejected",
        payload={"rejected_record": "corrupted_payload_string"},
    )


@pytest.mark.asyncio
async def test_vision_pipeline_dispatch_sub_batch_success() -> None:
    """Validates the execution flow of uploading parquets and routing the resulting S3 URIs."""
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_router = AsyncMock()

    mock_transit.upload_batch = AsyncMock(
        return_value="s3://galadril/test.parquet"
    )

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
        pipeline_router=mock_router,
        global_batch_timeout_s=15.0,
    )

    route_key = PipelineRouteKey(tenant_id="tenant-x", topic="telemetry")
    records = [{"data": 1}]

    success = await pipeline._dispatch_sub_batch(route_key, records)

    assert success is True
    mock_transit.upload_batch.assert_called_once_with(
        key=ANY, records=records, format_type="parquet"
    )
    mock_router.dispatch_parquet.assert_called_once_with(
        route_key=route_key,
        parquet_uri="s3://galadril/test.parquet",
        fallback_timeout_s=15.0,
    )


@pytest.mark.asyncio
@patch("galadril_vision.pipeline.runner.validate_and_normalize_kafka_batch")
async def test_vision_pipeline_partial_failure_routes_to_dlq(
    mock_validate: MagicMock,
) -> None:
    """Verifies that when a sub-batch dispatch fails, the main process reports False and sends data to DLQ."""
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_router = AsyncMock()
    mock_dlq_producer = AsyncMock()

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
        pipeline_router=mock_router,
        dlq_producer=mock_dlq_producer,
        dlq_topic="authz-dlq",
    )

    mock_record = MagicMock()
    failed_payload = {"tenant_id": "tenant-fail", "topic": "raw"}
    mock_record.model_dump.return_value = failed_payload

    mock_validated = MagicMock()
    mock_validated.accepted = [mock_record]
    mock_validated.rejected = []
    mock_validate.return_value = mock_validated

    pipeline._dispatch_sub_batch = AsyncMock(return_value=False)

    success = await pipeline.process_batch([MagicMock()])

    assert success is False
    mock_dlq_producer.produce_json.assert_called_once_with(
        topic="authz-dlq",
        key="tenant-fail",
        payload=failed_payload,
    )


@pytest.mark.asyncio
async def test_vision_pipeline_run_loop_execution() -> None:
    """Ensures consumer loops process incoming messages and commit offsets continuously until stopped."""
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_router = AsyncMock()

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
        pipeline_router=mock_router,
    )

    mock_consumer.poll_batch.side_effect = [[MagicMock()], []]
    pipeline.process_batch = AsyncMock(return_value=True)

    stop_event = asyncio.Event()

    async def trigger_stop():
        await asyncio.sleep(0.02)
        stop_event.set()

    await asyncio.gather(pipeline.run(stop_event=stop_event), trigger_stop())

    assert mock_consumer.poll_batch.call_count >= 2
    pipeline.process_batch.assert_called_once()
    mock_consumer.commit.assert_called_once()
