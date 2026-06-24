"""Kafka multi-topic async consumer with non-blocking event loop execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from confluent_kafka.aio import AIOConsumer
from confluent_kafka.schema_registry._async.avro import AsyncAvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

from galadril_vision.connectors.kafka.resolver import DynamicEventResolver


@dataclass(frozen=True, slots=True)
class IngestedMessage:
    topic: str
    payload: dict[str, Any]
    event_type: str

    def __iter__(self):
        yield self.topic
        yield self.payload


class KafkaMultiTopicConsumer:
    """Consumes messages asynchronously from multiple Kafka topics with dynamic schemas."""

    def __init__(
        self,
        kafka_cfg: Any,
        topics: list[str],
        schema_registry_url: str,
        sources: list[Any],
    ) -> None:
        """Initializes the async consumer with native AIO components."""
        self._topics = topics
        self._schema_registry_url = schema_registry_url
        self._deserializers: dict[str, AsyncAvroDeserializer] = {}

        self._resolver = DynamicEventResolver(
            sources=sources, schema_registry_url=schema_registry_url
        )

        consumer_conf = {
            "bootstrap.servers": kafka_cfg.bootstrap_servers,
            "group.id": kafka_cfg.group_id,
            "auto.offset.reset": kafka_cfg.auto_offset_reset,
            "enable.auto.commit": kafka_cfg.enable_auto_commit,
        }
        self._consumer = AIOConsumer(consumer_conf)

    async def connect(self) -> None:
        """Subscribes the consumer client and instantiates async deserializers."""
        await self._consumer.subscribe(self._topics)

        for topic in self._topics:
            # The @asyncinit decorator on AsyncAvroDeserializer obfuscates the __init_impl__
            # signature from static type checkers, requiring a localized type ignore.
            self._deserializers[topic] = await AsyncAvroDeserializer(
                self._resolver.registry_client  # type: ignore
            )

    async def poll_event(self, timeout: float = 1.0) -> dict[str, Any] | None:
        """Polls Kafka asynchronously for a single message, resolving event metadata.

        Args:
            timeout: Maximum time allocation allowed to wait for an incoming payload.

        Returns:
            A structured dict carrying metadata context and payload, or None.
        """
        msg = await self._consumer.poll(timeout)
        if msg is None or msg.error():
            return None

        raw_value = msg.value()
        if not raw_value:
            return None

        event_type = await self._resolver.resolve_event_type(raw_value)

        ctx = SerializationContext(msg.topic(), MessageField.VALUE)
        payload = await self._deserializers[msg.topic()](raw_value, ctx)

        return {
            "event_type": event_type,
            "topic": msg.topic(),
            "key": msg.key().decode("utf-8") if msg.key() else None,
            "payload": payload,
        }

    async def poll_batch(
        self,
        max_messages: int = 100,
        timeout_s: float = 1.0,
    ) -> list[IngestedMessage]:
        """Polls and compiles a batch of messages within strict deadline constraints.

        Employs localized sequential asynchronous resolution to optimize memory overhead.
        """
        batch: list[IngestedMessage] = []
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        while len(batch) < max_messages:
            elapsed = loop.time() - start_time
            remaining = timeout_s - elapsed
            if remaining <= 0:
                break

            poll_timeout = (
                remaining if len(batch) == 0 else min(remaining, 0.01)
            )

            try:
                msg = await self._consumer.poll(timeout=poll_timeout)
                if msg is None:
                    if len(batch) > 0:
                        break
                    continue

                if msg.error():
                    continue

                topic = msg.topic()
                raw_value = msg.value()
                if not raw_value:
                    continue

                event_type = await self._resolver.resolve_event_type(raw_value)
                ctx = SerializationContext(topic, MessageField.VALUE)
                payload = await self._deserializers[topic](raw_value, ctx)

                if isinstance(payload, dict):
                    batch.append(
                        IngestedMessage(
                            topic=topic,
                            payload=payload,
                            event_type=event_type,
                        )
                    )
            except Exception:
                continue

        return batch

    async def commit(self, asynchronous: bool = False) -> None:
        """Commits the offsets asynchronously without blocking the execution thread."""
        await self._consumer.commit(asynchronous=asynchronous)

    async def close(self) -> None:
        """Closes down active background connections safely."""
        await self._consumer.close()
