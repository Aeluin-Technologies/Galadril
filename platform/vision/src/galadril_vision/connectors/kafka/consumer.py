"""Kafka multi-topic consumer with dynamic event type resolution."""

from __future__ import annotations

from typing import Any

from confluent_kafka import Consumer
from confluent_kafka.serialization import MessageField, SerializationContext
from confluent_kafka.schema_registry.avro import AvroDeserializer

from galadril_vision.connectors.kafka.resolver import DynamicEventResolver


class KafkaMultiTopicConsumer:
    """Consumes messages from multiple Kafka topics and injects dynamic event type contexts."""

    def __init__(
        self,
        kafka_cfg: Any,
        topics: list[str],
        schema_registry_url: str,
        sources: list[Any],
    ) -> None:
        """Initializes the multi-topic Kafka consumer with dynamic resolution capabilities.

        Args:
            kafka_cfg: Kafka connector configuration containing group and broker configurations.
            topics: List of topics to subscribe to.
            schema_registry_url: Endpoint for resolving schema data.
            sources: Configured sources list to initialize the schema resolver maps.
        """
        self._topics = topics
        self._schema_registry_url = schema_registry_url
        self._deserializers: dict[str, AvroDeserializer] = {}

        self._resolver = DynamicEventResolver(
            sources=sources, schema_registry_url=schema_registry_url
        )

        consumer_conf = {
            "bootstrap.servers": kafka_cfg.bootstrap_servers,
            "group.id": kafka_cfg.group_id,
            "auto.offset.reset": kafka_cfg.auto_offset_reset,
            "enable.auto.commit": kafka_cfg.enable_auto_commit,
        }
        self._consumer = Consumer(consumer_conf)
        self._init_deserializers()

    def _init_deserializers(self) -> None:
        """Pre-allocates standard Avro deserializers per targeted topic."""
        for topic in self._topics:
            self._deserializers[topic] = AvroDeserializer(
                schema_registry_client=self._resolver.registry_client
            )

    def connect(self) -> None:
        """Subscribes the consumer client to the configured topics."""
        self._consumer.subscribe(self._topics)

    def poll_event(self, timeout: float = 1.0) -> dict[str, Any] | None:
        """Polls Kafka for messages, detects event type from schema headers, and deserializes payload.

        Args:
            timeout: Maximum time allocation allowed to wait for incoming payloads.

        Returns:
            A structured dict carrying metadata context and payload, or None if empty.
        """
        msg = self._consumer.poll(timeout)
        if msg is None or msg.error():
            return None

        raw_value = msg.value()
        if not raw_value:
            return None

        event_type = self._resolver.resolve_event_type(raw_value)

        ctx = SerializationContext(msg.topic(), MessageField.VALUE)
        payload = self._deserializers[msg.topic()](raw_value, ctx)

        return {
            "event_type": event_type,
            "topic": msg.topic(),
            "key": msg.key().decode("utf-8") if msg.key() else None,
            "payload": payload,
        }

    def poll_batch(
        self,
        max_messages: int = 100,
        timeout_s: float = 1.0,
    ) -> list[tuple[str, dict[str, Any], str]]:
        """Polls a batch of messages, resolving their schemas dynamically.

        Returns:
            A list of tuples: (topic_name, deserialized_payload, resolved_event_type)
        """
        batch: list[tuple[str, dict[str, Any], str]] = []
        messages = self._consumer.consume(
            num_messages=max_messages, timeout=timeout_s
        )

        for msg in messages:
            if msg.error():
                continue

            topic = msg.topic()
            raw_value = msg.value()
            if not raw_value:
                continue

            try:
                event_type = self._resolver.resolve_event_type(raw_value)
                ctx = SerializationContext(topic, MessageField.VALUE)
                payload = self._deserializers[topic](raw_value, ctx)

                if isinstance(payload, dict):
                    batch.append((topic, payload, event_type))
            except Exception:
                continue

        return batch

    def close(self) -> None:
        """Closes down active network connections safely."""
        self._consumer.close()
