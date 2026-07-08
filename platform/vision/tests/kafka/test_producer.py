"""Unit tests targeting the JSON producer, DLQ resolution, and Admin topic generation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from confluent_kafka.cimpl import KafkaException
from galadril_vision.common.config import KafkaConnectorConfig
from galadril_vision.connectors.kafka.producer import (
    KafkaJsonProducer,
    KafkaTopicSpec,
    ensure_topics,
    resolve_authz_dlq_topic,
)


@pytest.fixture
def mock_kafka_connector_config() -> MagicMock:
    """Constructs a basic Kafka infrastructure configuration layout."""
    cfg = MagicMock(spec=KafkaConnectorConfig)
    cfg.bootstrap_servers = "localhost:9092"
    cfg.authz_dlq_topic = "custom.authz.dlq"
    return cfg


def test_resolve_authz_dlq_topic_fallbacks(
    mock_kafka_connector_config: MagicMock,
) -> None:
    """Verifies fallback patterns when resolving the target DLQ topic configuration parameter."""
    assert (
        resolve_authz_dlq_topic(mock_kafka_connector_config)
        == "custom.authz.dlq"
    )

    mock_kafka_connector_config.authz_dlq_topic = "   "
    assert (
        resolve_authz_dlq_topic(mock_kafka_connector_config)
        == "galadril.authz.dlq"
    )

    mock_kafka_connector_config.authz_dlq_topic = None
    assert (
        resolve_authz_dlq_topic(mock_kafka_connector_config)
        == "galadril.authz.dlq"
    )


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.producer.AdminClient")
async def test_ensure_topics_empty_and_nominal(
    mock_admin_cls: MagicMock,
) -> None:
    """Verifies AdminClient bypassing or topic deployment routines depending on parameter inputs."""
    await ensure_topics(bootstrap_servers="localhost:9092", topics=[])
    mock_admin_cls.assert_not_called()

    mock_admin = MagicMock()
    mock_admin_cls.return_value = mock_admin

    mock_future = MagicMock()
    mock_future.result.return_value = None
    mock_admin.create_topics.return_value = {"topic-a": mock_future}

    specs = [KafkaTopicSpec(name="topic-a", partitions=3, replication_factor=1)]
    await ensure_topics(bootstrap_servers="localhost:9092", topics=specs)

    mock_admin.create_topics.assert_called_once()
    mock_future.result.assert_called_once()


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.producer.AdminClient")
async def test_ensure_topics_exception_handling(
    mock_admin_cls: MagicMock,
) -> None:
    """Validates resilience against network request timeouts and ignore patterns for existing topics."""
    mock_admin = MagicMock()
    mock_admin_cls.return_value = mock_admin

    mock_admin.create_topics.side_effect = RuntimeError("Network partition")
    specs = [KafkaTopicSpec(name="topic-b")]
    await ensure_topics(bootstrap_servers="localhost:9092", topics=specs)

    mock_admin.create_topics.side_effect = None
    mock_fut_exists = MagicMock()
    mock_error = MagicMock()
    mock_error.str.return_value = "TOPIC_ALREADY_EXISTS error"
    mock_fut_exists.result.side_effect = KafkaException(mock_error)

    mock_fut_fail = MagicMock()
    mock_fut_fail.result.side_effect = ValueError("Fatal crash")

    mock_admin.create_topics.return_value = {
        "topic-exists": mock_fut_exists,
        "topic-fail": mock_fut_fail,
    }
    await ensure_topics(
        bootstrap_servers="localhost:9092",
        topics=[KafkaTopicSpec(name="x"), KafkaTopicSpec(name="y")],
    )


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.producer.AIOProducer")
async def test_kafka_json_producer_emission_and_flush(
    mock_producer_cls: MagicMock, mock_kafka_connector_config: MagicMock
) -> None:
    """Ensures serializations match binary specifications and maps downstream flush time boundaries."""
    mock_producer = MagicMock()
    mock_producer.produce = AsyncMock()
    mock_producer.flush = AsyncMock()
    mock_producer_cls.return_value = mock_producer

    producer = KafkaJsonProducer(cfg=mock_kafka_connector_config)
    await producer.produce_json(topic="out", key="k", payload={"val": 42})

    mock_producer.produce.assert_called_once_with(
        topic="out", key="k", value=b'{"val":42}'
    )

    await producer.flush(timeout_s=2.5)
    mock_producer.flush.assert_called_once_with(2.5)
