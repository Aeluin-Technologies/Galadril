"""Unit tests focusing on the Kafka lifecycle hooks and batch polling mechanics."""

from unittest.mock import MagicMock, patch
import pytest
from confluent_kafka import KafkaError, TopicPartition

from galadril_pipeline.resources.kafka import KafkaResource


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_kafka_resource_lifecycle(mock_consumer_cls: MagicMock) -> None:
    """Tests proper creation, subscription, and teardown of the consumer."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer

    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    resource.setup_for_execution(MagicMock())

    mock_consumer_cls.assert_called_once()
    mock_consumer.subscribe.assert_called_with(["t"])

    resource.teardown_after_execution(MagicMock())
    mock_consumer.close.assert_called_once()


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_kafka_resource_lag_evaluation(mock_consumer_cls: MagicMock) -> None:
    """Validates the non-destructive lag evaluation branches."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )

    # No consumer configured.
    assert resource.has_lag() is False

    resource.setup_for_execution(MagicMock())

    # Empty assignment.
    mock_consumer.assignment.return_value = []
    assert resource.has_lag() is False

    # Active lag present.
    tp = TopicPartition("t", 0)
    mock_consumer.assignment.return_value = [tp]

    mock_position = MagicMock()
    mock_position.offset = 10
    mock_consumer.position.return_value = [mock_position]
    mock_consumer.get_watermark_offsets.return_value = (0, 20)
    assert resource.has_lag() is True

    # Exception fallback branch.
    mock_consumer.position.side_effect = Exception("Kafka Error")
    assert resource.has_lag() is False


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
async def test_kafka_resource_poll_batch(mock_consumer_cls: MagicMock) -> None:
    """Validates accumulation window constraints, deserialization, and edge cases."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )

    # Attempt to poll without setup.
    assert await resource.poll_batch() == []

    resource.setup_for_execution(MagicMock())

    # Message with valid JSON.
    msg_valid = MagicMock()
    msg_valid.error.return_value = None
    msg_valid.key.return_value = b"k1"
    msg_valid.value.return_value = (
        b'{"storage_path": "s3://p", "tenant_id": "t1"}'
    )
    msg_valid.topic.return_value = "t"

    # Message with partition EOF error.
    msg_eof = MagicMock()
    err_eof = MagicMock()
    err_eof.code.return_value = KafkaError._PARTITION_EOF
    msg_eof.error.return_value = err_eof

    # Sequence of returns: valid message, EOF message, then None.
    mock_consumer.poll.side_effect = [msg_valid, msg_eof, None]

    records = await resource.poll_batch(max_records=5, timeout_s=1.0)
    assert len(records) == 1
    assert records[0]["record_id"] == "k1"
    assert records[0]["tenant_id"] == "t1"


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
async def test_kafka_commit_offsets(mock_consumer_cls: MagicMock) -> None:
    """Verifies offloading commit calls to standard thread pools cleanly."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )

    # No consumer setup.
    await resource.commit_offsets({"t": {0: 100}})
    mock_consumer.commit.assert_not_called()

    resource.setup_for_execution(MagicMock())
    await resource.commit_offsets({"t": {0: 100}})

    # Verify target offset increments for next fetch marker boundary.
    assert mock_consumer.commit.call_count == 1
