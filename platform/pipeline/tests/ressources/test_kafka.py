"""Unit tests focusing on the Kafka lifecycle hooks and batch polling mechanics."""

from unittest.mock import MagicMock, patch

import pytest
from confluent_kafka import KafkaError, TopicPartition
from galadril_pipeline.resources.kafka import KafkaResource


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_kafka_resource_lifecycle(mock_consumer_cls: MagicMock) -> None:
    """Tests proper creation, subscription configuration, and teardown of the consumer client."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer

    resource = KafkaResource(
        bootstrap_servers="localhost:9092",
        group_id="vision-group",
        topics=["raw-events", "telemetry"],
    )

    resource.setup_for_execution(MagicMock())

    mock_consumer_cls.assert_called_once_with(
        {
            "bootstrap.servers": "localhost:9092",
            "group.id": "vision-group",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    mock_consumer.subscribe.assert_called_once_with(["raw-events", "telemetry"])

    resource.teardown_after_execution(MagicMock())
    mock_consumer.close.assert_called_once()


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_kafka_resource_lag_evaluation(mock_consumer_cls: MagicMock) -> None:
    """Validates high watermark partition comparison mechanics for consumer lag tracking."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer

    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )

    assert resource.has_lag() is False

    resource.setup_for_execution(MagicMock())

    mock_consumer.assignment.return_value = []
    assert resource.has_lag() is False

    tp = TopicPartition("t", 0)
    mock_consumer.assignment.return_value = [tp]

    mock_position = MagicMock()
    mock_position.offset = 10
    mock_consumer.position.return_value = [mock_position]
    mock_consumer.get_watermark_offsets.return_value = (0, 20)

    assert resource.has_lag() is True

    mock_consumer.position.side_effect = Exception(
        "Broker disconnect simulation"
    )
    assert resource.has_lag() is False


def test_kafka_resource_get_current_offsets_uninitialized() -> None:
    """Validates offset collection loops safely fall back to an empty dictionary state when uninitialized."""
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    assert resource.get_current_offsets() == {}


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_kafka_resource_get_current_offsets_operational(
    mock_consumer_cls: MagicMock,
) -> None:
    """Validates that active consumer positions parse accurately into decremented offset structures (offset - 1)."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer

    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    resource.setup_for_execution(MagicMock())

    tp1 = TopicPartition("topic_a", 0, 50)
    tp2 = TopicPartition("topic_a", 1, 100)
    mock_consumer.assignment.return_value = [tp1, tp2]
    mock_consumer.position.return_value = [tp1, tp2]

    offsets = resource.get_current_offsets()
    assert offsets == {"topic_a": {0: 49, 1: 99}}


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
async def test_kafka_resource_poll_batch(mock_consumer_cls: MagicMock) -> None:
    """Validates message batch accumulation limits, deserialization layers, and error parsing boundaries."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer

    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    resource.setup_for_execution(MagicMock())

    msg_valid = MagicMock()
    msg_valid.error.return_value = None
    msg_valid.key.return_value = b"record_hash_xyz"
    msg_valid.value.return_value = b'{"storage_path": "s3://staging/file.parquet", "tenant_id": "tenant_4"}'
    msg_valid.topic.return_value = "t"

    msg_eof = MagicMock()
    err_eof = MagicMock()
    err_eof.code.return_value = KafkaError._PARTITION_EOF
    msg_eof.error.return_value = err_eof

    mock_consumer.poll.side_effect = [msg_valid, msg_eof, None]

    records = await resource.poll_batch(max_records=5, timeout_s=2.0)

    assert len(records) == 1
    assert records[0]["record_id"] == "record_hash_xyz"
    assert records[0]["storage_path"] == "s3://staging/file.parquet"
    assert records[0]["tenant_id"] == "tenant_4"
    assert records[0]["source"] == "t"


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
async def test_kafka_commit_offsets(mock_consumer_cls: MagicMock) -> None:
    """Verifies offset dictionary parameters increment and handoff execution to synchronous thread pools cleanly."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer

    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    resource.setup_for_execution(MagicMock())

    input_offsets = {"topic_b": {0: 99, 1: 199}}
    await resource.commit_offsets(input_offsets)

    expected_tp_list = mock_consumer.commit.call_args[1]["offsets"]
    assert len(expected_tp_list) == 2
    assert expected_tp_list[0].topic == "topic_b"
    assert expected_tp_list[0].partition == 0
    assert expected_tp_list[0].offset == 100
    assert expected_tp_list[1].offset == 200
