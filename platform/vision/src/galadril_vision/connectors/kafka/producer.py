"""FastStream-backed JSON publisher adapter for authorization outbox events."""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue

from galadril_vision.common.config import KafkaConnectorConfig

_DEFAULT_AUTHZ_DLQ_TOPIC = "galadril.authz.dlq"


class BrokerPublisher(Protocol):
    """Subset of FastStream KafkaBroker used by the outbox adapter."""

    async def publish(
        self,
        message: object,
        topic: str,
        *,
        key: bytes | str | None = None,
        no_confirm: bool = False,
    ) -> object: ...


def resolve_authz_dlq_topic(cfg: KafkaConnectorConfig) -> str:
    """Resolves the authorization DLQ topic with a stable fallback."""
    topic = (cfg.authz_dlq_topic or "").strip()
    return topic if topic else _DEFAULT_AUTHZ_DLQ_TOPIC


class KafkaJsonProducer:
    """Publishes JSON through the process-owned instrumented FastStream broker."""

    __slots__ = ("_broker",)

    def __init__(self, broker: BrokerPublisher) -> None:
        self._broker = broker

    async def produce_json(
        self, *, topic: str, key: str, payload: dict[str, JsonValue]
    ) -> None:
        """Performs a confirmed publish with automatic W3C header injection."""
        await self._broker.publish(
            payload,
            topic,
            key=key,
            no_confirm=False,
        )
