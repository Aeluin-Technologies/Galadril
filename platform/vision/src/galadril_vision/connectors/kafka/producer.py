"""Kafka JSON producer and management utilities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import orjson
import structlog
from confluent_kafka.admin import AdminClient
from confluent_kafka.aio import AIOProducer
from confluent_kafka.cimpl import KafkaException, NewTopic

from galadril_vision.common.config import KafkaConnectorConfig

logger = structlog.get_logger(__name__)

_DEFAULT_AUTHZ_DLQ_TOPIC = "galadril.authz.dlq"


@dataclass(frozen=True, slots=True)
class KafkaTopicSpec:
    """Defines the creation parameters for a Kafka topic."""

    name: str
    partitions: int = 1
    replication_factor: int = 1


def resolve_authz_dlq_topic(cfg: KafkaConnectorConfig) -> str:
    """Resolves the authorization DLQ topic name with a default fallback.

    Args:
        cfg: Configuration containing the target topic name.

    Returns:
        The verified topic name string.
    """
    t = (cfg.authz_dlq_topic or "").strip()
    return t if t else _DEFAULT_AUTHZ_DLQ_TOPIC


async def ensure_topics(
    *,
    bootstrap_servers: str,
    topics: list[KafkaTopicSpec],
    request_timeout_s: float = 5.0,
) -> None:
    """Creates the specified Kafka topics if they do not already exist.

    Args:
        bootstrap_servers: Kafka broker connection string.
        topics: List of topic definitions to create.
        request_timeout_s: Network timeout duration for the admin request.
    """
    if not topics:
        return

    def _sync_create_topics() -> None:
        admin = AdminClient({"bootstrap.servers": bootstrap_servers})
        new_topics = [
            NewTopic(
                t.name,
                num_partitions=t.partitions,
                replication_factor=t.replication_factor,
            )
            for t in topics
        ]

        try:
            fs = admin.create_topics(
                new_topics, request_timeout=request_timeout_s
            )
        except Exception as exc:
            logger.warning("kafka_create_topics_request_failed", error=str(exc))
            return

        for name, fut in fs.items():
            try:
                fut.result()
                logger.info("kafka_topic_created", topic=name)
            except KafkaException as exc:
                msg = str(exc)
                if "TOPIC_ALREADY_EXISTS" in msg or "TopicAlreadyExists" in msg:
                    continue
                logger.warning(
                    "kafka_topic_create_failed", topic=name, error=msg
                )
            except Exception as exc:
                logger.warning(
                    "kafka_topic_create_failed", topic=name, error=str(exc)
                )

    await asyncio.to_thread(_sync_create_topics)


class KafkaJsonProducer:
    """Produces JSON serialized messages to Kafka topics."""

    def __init__(self, cfg: KafkaConnectorConfig) -> None:
        """Initializes the producer.

        Args:
            cfg: Configuration object containing bootstrap server addresses.
        """
        self._cfg = cfg
        self._producer = AIOProducer(
            {"bootstrap.servers": cfg.bootstrap_servers}
        )

    async def produce_json(
        self, *, topic: str, key: str, payload: dict[str, Any]
    ) -> None:
        """Serializes and queues a JSON message payload for delivery.

        Args:
            topic: Destination topic name.
            key: Message record key identifier.
            payload: Dictionary payload to serialize.
        """
        data = orjson.dumps(payload)
        await self._producer.produce(topic=topic, key=key, value=data)

    async def flush(self, timeout_s: float = 10.0) -> None:
        """Blocks until all queued messages are successfully delivered or timeout occurs.

        Args:
            timeout_s: Maximum duration to wait before aborting.
        """
        await self._producer.flush(timeout_s)
