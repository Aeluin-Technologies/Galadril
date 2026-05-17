"""Kafka consumer for dynamic topics."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import orjson
import structlog
from confluent_kafka import Consumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer

from galadril_vision.common.exceptions import KafkaConsumerError

if TYPE_CHECKING:
    from galadril_vision.common.config import KafkaConfig

logger = structlog.get_logger(__name__)


class KafkaMultiTopicConsumer:
    """Consume heterogeneous messages from multiple Kafka topics."""

    def __init__(
        self,
        config: KafkaConfig,
        topics: list[str],
        schema_registry_url: str | None = None,
    ) -> None:
        self._config = config
        self._topics = topics
        self._consumer: Consumer | None = None
        self._schema_registry_url = schema_registry_url
        self._deserializers: dict[str, AvroDeserializer] = {}

    def connect(self) -> None:
        """Initialize the Kafka consumer."""
        if not self._topics:
            logger.warning("no_topics_to_subscribe")
            return

        conf = {
            "bootstrap.servers": self._config.bootstrap_servers,
            "group.id": self._config.group_id,
            "auto.offset.reset": self._config.auto_offset_reset,
            "enable.auto.commit": self._config.enable_auto_commit,
            "session.timeout.ms": self._config.session_timeout_ms,
        }

        self._consumer = Consumer(conf)
        self._consumer.subscribe(self._topics)

        if self._schema_registry_url:
            self._init_deserializers()

        logger.info(
            "kafka_consumer_connected",
            topics=self._topics,
            group_id=self._config.group_id,
        )

    def _init_deserializers(self) -> None:
        """Initialize Avro deserializers for each topic."""
        SchemaRegistryClient({"url": self._schema_registry_url})
        for topic in self._topics:
            try:
                self._deserializers[topic] = AvroDeserializer()
            except Exception as exc:
                logger.warning(
                    "deserializer_init_failed",
                    topic=topic,
                    error=str(exc),
                )

    def poll_batch(
        self,
        max_records: int | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Poll for a batch of messages."""
        if self._consumer is None:
            raise KafkaConsumerError("Consumer not connected.")

        batch_size = max_records or self._config.max_poll_records
        records: list[tuple[str, dict[str, Any]]] = []

        while len(records) < batch_size:
            msg = self._consumer.poll(timeout=1.0)

            if msg is None:
                break

            if msg.error():
                raise KafkaConsumerError(f"Kafka error: {msg.error()}")

            msg_topic = msg.topic()
            msg_value = msg.value()

            if msg_topic is not None and msg_value is not None:
                try:
                    if msg_topic in self._deserializers:
                        payload = self._deserializers[msg_topic](
                            msg_value, None
                        )
                    else:
                        payload = orjson.loads(msg_value)
                    records.append((msg_topic, payload))
                except Exception as exc:
                    logger.warning(
                        "message_parse_failed", topic=msg_topic, error=str(exc)
                    )

        return records

    def commit(self) -> None:
        """Commit current offsets."""
        if self._consumer:
            self._consumer.commit(asynchronous=False)

    def close(self) -> None:
        """Close the consumer connection."""
        if self._consumer:
            self._consumer.close()
            self._consumer = None
            logger.info("kafka_consumer_closed")

    def __enter__(self) -> "KafkaMultiTopicConsumer":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def stream(self) -> Iterator[list[tuple[str, dict[str, Any]]]]:
        """Yield batches of records continuously."""
        while True:
            batch = self.poll_batch()
            if batch:
                yield batch
