"""Unit tests targeting the Dagster GraphQL client and the streaming pipeline orchestrator."""

import pytest
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from galadril_vision.pipeline.client import DagsterAsyncClient
from galadril_vision.pipeline.runner import VisionPipeline
from galadril_vision.connectors.kafka.consumer import IngestedMessage


@pytest.fixture
def mock_aiohttp_session() -> Generator[MagicMock, None, None]:
    """Intercepts and mocks aiohttp ClientSession context manager operations."""
    with patch("aiohttp.ClientSession") as mock_session_cls:
        session_instance = MagicMock()
        mock_session_cls.return_value.__aenter__.return_value = session_instance
        yield session_instance


@pytest.mark.asyncio
async def test_dagster_client_trigger_job_success(
    mock_aiohttp_session: MagicMock,
) -> None:
    """Validates successful job execution when Dagster accepts the GraphQL mutation."""
    client = DagsterAsyncClient(endpoint_url="http://localhost:3000/graphql")

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json.return_value = {
        "data": {
            "launchPipelineExecution": {
                "__typename": "LaunchRunSuccess",
                "run": {"runId": "run-12345"},
            }
        }
    }
    mock_aiohttp_session.post.return_value.__aenter__.return_value = (
        mock_response
    )

    success = await client.trigger_job(
        "vision_job", "s3://bucket/batch.parquet"
    )
    assert success is True


@pytest.mark.asyncio
async def test_dagster_client_graphql_error_handling(
    mock_aiohttp_session: MagicMock,
) -> None:
    """Ensures client returns False when GraphQL returns explicit query errors."""
    client = DagsterAsyncClient()

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json.return_value = {"errors": [{"message": "Syntax Error"}]}
    mock_aiohttp_session.post.return_value.__aenter__.return_value = (
        mock_response
    )

    success = await client.trigger_job(
        "vision_job", "s3://bucket/batch.parquet"
    )
    assert success is False


@pytest.mark.asyncio
@patch(
    "galadril_vision.pipeline.orchestrator.validate_and_normalize_kafka_batch"
)
async def test_vision_pipeline_batch_processing_routing(
    mock_validate: MagicMock, mock_aiohttp_session: MagicMock
) -> None:
    """Verifies message partitioning by tenant/topic and subsequent dispatch sequencing."""
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_dagster = AsyncMock()
    mock_dlq_producer = AsyncMock()

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
        dagster_client=mock_dagster,
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

    mock_transit.upload_batch.return_value = "s3://transit/batch.parquet"
    mock_dagster.trigger_job.return_value = True

    input_batch = [
        IngestedMessage(topic="vision-frames", payload={}, event_type="FRAME")
    ]
    success = await pipeline.process_batch(input_batch)

    assert success is True
    mock_transit.upload_batch.assert_called_once()
    mock_dagster.trigger_job.assert_called_once_with(
        job_name="vision_pipeline_job",
        batch_storage_path="s3://transit/batch.parquet",
    )


@pytest.mark.asyncio
@patch(
    "galadril_vision.pipeline.orchestrator.validate_and_normalize_kafka_batch"
)
async def test_vision_pipeline_rejected_records_sent_to_dlq(
    mock_validate: MagicMock,
) -> None:
    """Guarantees that unparseable or rejected records are isolated and routed to the DLQ."""
    mock_consumer = AsyncMock()
    mock_transit = AsyncMock()
    mock_dagster = AsyncMock()
    mock_dlq_producer = AsyncMock()

    pipeline = VisionPipeline(
        consumer=mock_consumer,
        transit_service=mock_transit,
        dagster_client=mock_dagster,
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
