"""Unit tests targeting the async multi-topic Kafka Avro consumer."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from galadril_vision.connectors.kafka.consumer import (
    KafkaMultiTopicConsumer,
    IngestedMessage,
)


class FakeKafkaConfig:
    """Stub providing parameters required for confluent-kafka client options."""

    bootstrap_servers = "localhost:9092"
    group_id = "test-group"
    auto_offset_reset = "earliest"
    enable_auto_commit = False


@pytest.fixture
def mock_kafka_config() -> FakeKafkaConfig:
    """Provides standard Kafka connection parameters."""
    return FakeKafkaConfig()


def test_ingested_message_dataclass_unpacking() -> None:
    """Validates structural properties and unpack behavior of the IngestedMessage dataclass."""
    msg = IngestedMessage(
        topic="test-topic", payload={"data": 1}, event_type="test_source"
    )
    assert msg.topic == "test-topic"
    assert msg.payload == {"data": 1}
    assert msg.event_type == "test_source"

    topic, payload = msg
    assert topic == "test-topic"
    assert payload == {"data": 1}


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.consumer.AIOConsumer")
@patch("galadril_vision.connectors.kafka.consumer.DynamicEventResolver")
@patch("galadril_vision.connectors.kafka.consumer.AsyncAvroDeserializer")
async def test_consumer_lifecycle_connect(
    mock_deserializer_cls: MagicMock,
    mock_resolver_cls: MagicMock,
    mock_consumer_cls: MagicMock,
    mock_kafka_config: FakeKafkaConfig,
) -> None:
    """Verifies successful topic subscriptions and Avro deserializer environment initialization."""
    mock_consumer = MagicMock()
    mock_consumer.subscribe = AsyncMock()
    mock_consumer_cls.return_value = mock_consumer

    mock_deserializer = AsyncMock()
    mock_deserializer_cls.return_value = mock_deserializer

    consumer = KafkaMultiTopicConsumer(
        kafka_cfg=mock_kafka_config,
        topics=["topic1", "topic2"],
        schema_registry_url="http://registry:8081",
        sources=[],
    )

    await consumer.connect()
    mock_consumer.subscribe.assert_called_once_with(["topic1", "topic2"])
    assert mock_deserializer_cls.call_count == 2


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.consumer.AIOConsumer")
@patch("galadril_vision.connectors.kafka.consumer.DynamicEventResolver")
async def test_poll_event_empty_and_errors(
    mock_resolver_cls: MagicMock,
    mock_consumer_cls: MagicMock,
    mock_kafka_config: FakeKafkaConfig,
) -> None:
    """Validates that empty messages or messages containing errors return None immediately."""
    mock_consumer = MagicMock()
    mock_consumer.poll = AsyncMock()
    mock_consumer_cls.return_value = mock_consumer

    consumer = KafkaMultiTopicConsumer(
        kafka_cfg=mock_kafka_config,
        topics=["topic1"],
        schema_registry_url="http://registry:8081",
        sources=[],
    )

    mock_consumer.poll.return_value = None
    res_none = await consumer.poll_event()
    assert res_none is None

    mock_error_msg = MagicMock()
    mock_error_msg.error.return_value = True
    mock_consumer.poll.return_value = mock_error_msg
    res_err = await consumer.poll_event()
    assert res_err is None

    mock_empty_msg = MagicMock()
    mock_empty_msg.error.return_value = False
    mock_empty_msg.value.return_value = b""
    mock_consumer.poll.return_value = mock_empty_msg
    res_empty = await consumer.poll_event()
    assert res_empty is None


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.consumer.AIOConsumer")
@patch("galadril_vision.connectors.kafka.consumer.DynamicEventResolver")
async def test_poll_event_success_decoding(
    mock_resolver_cls: MagicMock,
    mock_consumer_cls: MagicMock,
    mock_kafka_config: FakeKafkaConfig,
) -> None:
    """Validates fully extraction pipeline of metadata and binary fields during a successful poll."""
    mock_consumer = MagicMock()
    mock_consumer.poll = AsyncMock()
    mock_consumer_cls.return_value = mock_consumer

    consumer = KafkaMultiTopicConsumer(
        kafka_cfg=mock_kafka_config,
        topics=["topic1"],
        schema_registry_url="http://registry:8081",
        sources=[],
    )

    mock_msg = MagicMock()
    mock_msg.error.return_value = False
    mock_msg.value.return_value = b"\x00\x00\x00\x00\x01payload"
    mock_msg.topic.return_value = "topic1"
    mock_msg.key.return_value = b"message-key"
    mock_consumer.poll.return_value = mock_msg

    consumer._resolver.resolve_event_type = AsyncMock(
        return_value="image_source"
    )
    mock_deserializer = AsyncMock(return_value={"field": "value"})
    consumer._deserializers["topic1"] = mock_deserializer

    event = await consumer.poll_event()
    assert event is not None
    assert event["event_type"] == "image_source"
    assert event["topic"] == "topic1"
    assert event["key"] == "message-key"
    assert event["payload"] == {"field": "value"}


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.consumer.AIOConsumer")
@patch("galadril_vision.connectors.kafka.consumer.DynamicEventResolver")
async def test_poll_batch_accumulation_mechanics(
    mock_resolver_cls: MagicMock,
    mock_consumer_cls: MagicMock,
    mock_kafka_config: FakeKafkaConfig,
) -> None:
    """Tests the loop control limits, processing safety, and time window terminations of poll_batch."""
    mock_consumer = MagicMock()
    mock_consumer.poll = AsyncMock()
    mock_consumer_cls.return_value = mock_consumer

    consumer = KafkaMultiTopicConsumer(
        kafka_cfg=mock_kafka_config,
        topics=["topic1"],
        schema_registry_url="http://registry:8081",
        sources=[],
    )

    mock_msg_valid = MagicMock()
    mock_msg_valid.error.return_value = False
    mock_msg_valid.topic.return_value = "topic1"
    mock_msg_valid.value.return_value = b"data"

    mock_msg_invalid = MagicMock()
    mock_msg_invalid.error.return_value = True

    mock_consumer.poll.side_effect = [mock_msg_valid, mock_msg_invalid, None]

    consumer._resolver.resolve_event_type = AsyncMock(
        return_value="text_source"
    )
    mock_deserializer = AsyncMock(return_value={"text": "hello"})
    consumer._deserializers["topic1"] = mock_deserializer

    batch = await consumer.poll_batch(max_messages=5, timeout_s=0.5)
    assert len(batch) == 1
    assert batch[0].topic == "topic1"
    assert batch[0].payload == {"text": "hello"}


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.consumer.AIOConsumer")
@patch("galadril_vision.connectors.kafka.consumer.DynamicEventResolver")
async def test_consumer_commit_and_close(
    mock_resolver_cls: MagicMock,
    mock_consumer_cls: MagicMock,
    mock_kafka_config: FakeKafkaConfig,
) -> None:
    """Ensures proxy calls routing down to internal confluent-kafka engine handles commit and shutdown."""
    mock_consumer = MagicMock()
    mock_consumer.commit = AsyncMock()
    mock_consumer.close = AsyncMock()
    mock_consumer_cls.return_value = mock_consumer

    consumer = KafkaMultiTopicConsumer(
        kafka_cfg=mock_kafka_config,
        topics=["topic1"],
        schema_registry_url="http://registry:8081",
        sources=[],
    )

    await consumer.commit(asynchronous=True)
    mock_consumer.commit.assert_called_once_with(asynchronous=True)

    await consumer.close()
    mock_consumer.close.assert_called_once()
