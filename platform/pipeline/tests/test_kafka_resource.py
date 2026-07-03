"""Unit tests focusing on the reactive Kafka integration lifecycle and message boundaries."""

from unittest.mock import MagicMock, patch
import pytest

from galadril_pipeline.resources.kafka import KafkaResource


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_kafka_resource_lifecycle_and_lag(mock_consumer_cls: MagicMock) -> None:
    """Tests lifecycle hooks and non-destructive partition watermark lag evaluation."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer

    resource = KafkaResource(
        bootstrap_servers="localhost:9092",
        group_id="test-group",
        topics=["test-topic"],
    )

    resource.setup_for_execution(MagicMock())
    mock_consumer_cls.assert_called_once()
    mock_consumer.subscribe.assert_called_with(["test-topic"])

    # Simulate active uncommitted partition lag.
    mock_tp = MagicMock()
    mock_consumer.assignment.return_value = [mock_tp]
    mock_position = MagicMock()
    mock_position.offset = 50
    mock_consumer.position.return_value = [mock_position]
    mock_consumer.get_watermark_offsets.return_value = (0, 100)

    assert resource.has_lag() is True

    resource.teardown_after_execution(MagicMock())
    mock_consumer.close.assert_called_once()


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
async def test_kafka_resource_poll_batch(mock_consumer_cls: MagicMock) -> None:
    """Validates message collection window closing and translation mechanics within thread pools."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer

    mock_msg = MagicMock()
    mock_msg.error.return_value = None
    mock_msg.key.return_value = b"k1"
    mock_msg.value.return_value = {
        "storage_path": "s3://path",
        "tenant_id": "t1",
    }
    mock_msg.topic.return_value = "test-topic"
    mock_consumer.poll.return_value = mock_msg

    resource = KafkaResource(
        bootstrap_servers="localhost:9092", group_id="g1", topics=["t1"]
    )
    resource._consumer = mock_consumer

    records = await resource.poll_batch(max_records=1, timeout_s=0.5)
    assert len(records) == 1
    assert records[0]["record_id"] == "k1"
    assert records[0]["tenant_id"] == "t1"
    assert records[0]["source"] == "test-topic"
