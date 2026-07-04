"""Unit tests targeting the decoupled streaming pipeline orchestrator."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from galadril_vision.connectors.kafka.consumer import IngestedMessage
from galadril_vision.pipeline.runner import VisionPipeline


@pytest.mark.asyncio
@patch("galadril_vision.pipeline.runner.validate_and_normalize_kafka_batch")
async def test_vision_pipeline_batch_processing_routing(
    mock_validate: MagicMock,
) -> None:
    """Verifies message partitioning by tenant/topic and subsequent transit upload sequencing.

    Args:
        mock_validate: Mocked kafka batch validation utility framework.
    """
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_dlq_producer = AsyncMock()

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
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

    input_batch = [
        IngestedMessage(topic="vision-frames", payload={}, event_type="FRAME")
    ]
    success = await pipeline.process_batch(input_batch)

    assert success is True
    mock_transit.upload_batch.assert_called_once()


@pytest.mark.asyncio
@patch("galadril_vision.pipeline.runner.validate_and_normalize_kafka_batch")
async def test_vision_pipeline_rejected_records_sent_to_dlq(
    mock_validate: MagicMock,
) -> None:
    """Guarantees that unparseable or rejected records are isolated and routed to the DLQ.

    Args:
        mock_validate: Mocked kafka batch validation utility framework.
    """
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_dlq_producer = AsyncMock()

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
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
