"""Unit tests focusing on the Kafka lifecycle hooks and batch polling mechanics."""

from unittest.mock import MagicMock, patch
import pytest
from confluent_kafka import KafkaError, TopicPartition

from galadril_pipeline.resources.kafka import KafkaResource, VisionKafkaResource


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

    # Empty position edge-case tracking
    mock_consumer.position.return_value = []
    assert resource.has_lag() is False

    mock_consumer.position.side_effect = Exception("Kafka Error")
    assert resource.has_lag() is False


def test_kafka_resource_get_current_offsets_no_consumer() -> None:
    """Validates offset collection loops safely fall back to an empty dictionary state when uninitialized."""
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    assert resource.get_current_offsets() == {}


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_kafka_resource_get_current_offsets_active(
    mock_consumer_cls: MagicMock,
) -> None:
    """Validates that operational active offsets yield mapped dictionary structures accurately."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    resource.setup_for_execution(MagicMock())

    tp1 = TopicPartition("topic_a", 0)
    tp2 = TopicPartition("topic_a", 1)
    mock_consumer.assignment.return_value = [tp1, tp2]

    pos1 = MagicMock()
    pos1.offset = 50
    pos2 = MagicMock()
    pos2.offset = 100

    mock_consumer.position.side_effect = [[pos1], [pos2]]

    offsets = resource.get_current_offsets()
    assert offsets == {"topic_a": {0: 49, 1: 99}}


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
async def test_kafka_resource_poll_batch(mock_consumer_cls: MagicMock) -> None:
    """Validates accumulation window constraints, deserialization, and edge cases."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )

    assert await resource.poll_batch() == []

    resource.setup_for_execution(MagicMock())

    msg_valid = MagicMock()
    msg_valid.error.return_value = None
    msg_valid.key.return_value = b"k1"
    msg_valid.value.return_value = (
        b'{"storage_path": "s3://p", "tenant_id": "t1"}'
    )
    msg_valid.topic.return_value = "t"

    msg_eof = MagicMock()
    err_eof = MagicMock()
    err_eof.code.return_value = KafkaError._PARTITION_EOF
    msg_eof.error.return_value = err_eof

    msg_err = MagicMock()
    err_fatal = MagicMock()
    err_fatal.code.return_value = 999
    msg_err.error.return_value = err_fatal

    mock_consumer.poll.side_effect = [msg_valid, msg_eof, msg_err]

    records = await resource.poll_batch(max_records=5, timeout_s=1.0)
    assert len(records) == 1
    assert records[0]["record_id"] == "k1"
    assert records[0]["tenant_id"] == "t1"


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
@patch("asyncio.get_running_loop")
async def test_kafka_resource_poll_batch_timeout(
    mock_get_loop: MagicMock, mock_consumer_cls: MagicMock
) -> None:
    """Validates accumulation structures stop processing quickly upon expiration windows."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    resource.setup_for_execution(MagicMock())

    mock_loop = MagicMock()
    mock_loop.time.side_effect = [10.0, 15.0]
    mock_get_loop.return_value = mock_loop

    records = await resource.poll_batch(max_records=10, timeout_s=1.0)
    assert records == []


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
async def test_kafka_resource_poll_batch_corrupted_json(
    mock_consumer_cls: MagicMock,
) -> None:
    """Validates corrupted JSON exceptions drop cleanly without throwing processing exceptions."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )
    resource.setup_for_execution(MagicMock())

    msg_corrupt = MagicMock()
    msg_corrupt.error.return_value = None
    msg_corrupt.key.return_value = b"k2"
    msg_corrupt.value.return_value = b"invalid-json"

    mock_consumer.poll.side_effect = [msg_corrupt, None]
    records = await resource.poll_batch(max_records=2, timeout_s=1.0)
    assert records == []


@pytest.mark.asyncio
@patch("galadril_pipeline.resources.kafka.Consumer")
async def test_kafka_commit_offsets(mock_consumer_cls: MagicMock) -> None:
    """Verifies offloading commit calls to standard thread pools cleanly."""
    mock_consumer = MagicMock()
    mock_consumer_cls.return_value = mock_consumer
    resource = KafkaResource(
        bootstrap_servers="localhost", group_id="g", topics=["t"]
    )

    await resource.commit_offsets({"t": {0: 100}})
    mock_consumer.commit.assert_not_called()

    resource.setup_for_execution(MagicMock())
    await resource.commit_offsets({"t": {0: 100}})

    assert mock_consumer.commit.call_count == 1


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_vision_kafka_resource_inheritance(
    mock_consumer_cls: MagicMock,
) -> None:
    """Validates configuration parameter mapping structures within specialized infrastructure definitions."""
    mock_config_provider = MagicMock()
    cfg = mock_config_provider.vision_config
    cfg.kafka.bootstrap_servers = "broker:9092"
    cfg.kafka.group_id = "vision-group"
    cfg.get_kafka_topics.return_value = ["vision-raw"]

    resource = VisionKafkaResource(
        config_provider=mock_config_provider,
        bootstrap_servers="",
        group_id="",
        topics=[],
    )
    resource.setup_for_execution(MagicMock())

    assert resource.bootstrap_servers == "broker:9092"
    assert resource.group_id == "vision-group"
    assert resource.topics == ["vision-raw"]


@patch("galadril_pipeline.resources.kafka.Consumer")
def test_vision_kafka_resource_fallback_topics(
    mock_consumer_cls: MagicMock,
) -> None:
    """Validates default fallback definitions handle missing cluster topics gracefully."""
    mock_config_provider = MagicMock()
    cfg = mock_config_provider.vision_config
    cfg.get_kafka_topics.return_value = None

    resource = VisionKafkaResource(
        config_provider=mock_config_provider,
        bootstrap_servers="",
        group_id="",
        topics=[],
    )
    resource.setup_for_execution(MagicMock())

    assert resource.topics == ["raw"]
