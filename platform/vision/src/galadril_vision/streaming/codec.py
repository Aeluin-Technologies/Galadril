"""Confluent Avro decoding for FastStream without a second Kafka client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

import orjson
from confluent_kafka import Message
from confluent_kafka.schema_registry import AsyncSchemaRegistryClient
from confluent_kafka.schema_registry._async.avro import AsyncAvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext
from faststream.message import StreamMessage
from pydantic import JsonValue, TypeAdapter

from galadril_vision.connectors.kafka.resolver import DynamicEventResolver
from galadril_vision.streaming.handlers import AvroEnvelope

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class EventTypeResolver(Protocol):
    """Maps a Confluent wire schema ID to a configured source identifier."""

    registry_client: AsyncSchemaRegistryClient

    async def resolve_event_type(self, raw_bytes: bytes) -> str: ...

    async def close(self) -> None: ...


class AvroDeserializer(Protocol):
    """Async Schema Registry deserializer contract."""

    async def __call__(
        self, data: bytes, context: SerializationContext
    ) -> dict[str, object] | object | None: ...


DeserializerFactory = Callable[
    [AsyncSchemaRegistryClient], Awaitable[AvroDeserializer]
]


async def _create_deserializer(
    client: AsyncSchemaRegistryClient,
) -> AvroDeserializer:
    """Creates the Confluent async deserializer once per FastStream process."""
    return cast(AvroDeserializer, await AsyncAvroDeserializer(client))


class AvroMessageDecoder:
    """Decodes FastStream message bodies while preserving its ACK and OTel layers."""

    __slots__ = ("_deserializer", "_factory", "_lock", "_resolver")

    def __init__(
        self,
        *,
        sources: list[object],
        schema_registry_url: str,
        resolver: EventTypeResolver | None = None,
        deserializer_factory: DeserializerFactory = _create_deserializer,
    ) -> None:
        self._resolver = resolver or DynamicEventResolver(
            sources=sources,
            schema_registry_url=schema_registry_url,
        )
        self._factory = deserializer_factory
        self._deserializer: AvroDeserializer | None = None
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        message: object,
    ) -> dict[str, JsonValue]:
        """Returns a Pydantic-ready envelope from one Confluent-framed message."""
        if not isinstance(message, StreamMessage):
            raise TypeError("Avro decoder requires a FastStream message")
        typed_message = cast(StreamMessage[Message], message)
        if not isinstance(typed_message.body, bytes) or not typed_message.body:
            raise ValueError("Avro Kafka message body must be non-empty bytes")
        raw_message = typed_message.raw_message
        if isinstance(raw_message, tuple):
            raise TypeError("Avro decoder does not accept batch Kafka messages")
        topic = raw_message.topic()
        if not topic:
            raise ValueError("Kafka message is missing its topic")
        deserializer = await self._get_deserializer()
        source_id = await self._resolver.resolve_event_type(typed_message.body)
        if source_id == "UNKNOWN":
            raise ValueError(
                "Schema Registry ID is not mapped to a pipeline source"
            )
        payload = await deserializer(
            typed_message.body,
            SerializationContext(topic, MessageField.VALUE),
        )
        if not isinstance(payload, dict):
            raise ValueError("Avro payload must decode to an object")

        # Avro logical timestamps are datetime objects; normalize once at the
        # validation boundary so all later Kafka contracts remain plain JSON.
        json_payload = _JSON_OBJECT.validate_python(
            orjson.loads(orjson.dumps(payload, option=orjson.OPT_NAIVE_UTC))
        )
        envelope = AvroEnvelope(
            source_id=source_id,
            topic=topic,
            payload=json_payload,
        )
        return envelope.model_dump(mode="json")

    async def _get_deserializer(self) -> AvroDeserializer:
        """Initializes a single concurrency-safe Schema Registry decoder."""
        if self._deserializer is not None:
            return self._deserializer
        async with self._lock:
            if self._deserializer is None:
                self._deserializer = await self._factory(
                    self._resolver.registry_client
                )
        return self._deserializer

    async def close(self) -> None:
        """Closes the shared async Schema Registry client session."""
        await self._resolver.close()
