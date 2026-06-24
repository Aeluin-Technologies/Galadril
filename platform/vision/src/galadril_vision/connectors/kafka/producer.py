"""Kafka async producer utilities with non-blocking topic auto-creation."""

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
    name: str
    partitions: int = 1
    replication_factor: int = 1


def resolve_authz_dlq_topic(cfg: KafkaConnectorConfig) -> str:
    """Resolve the authz DLQ topic with a safe hardcoded fallback."""
    t = (cfg.authz_dlq_topic or "").strip()
    return t if t else _DEFAULT_AUTHZ_DLQ_TOPIC


async def ensure_topics(
    *,
    bootstrap_servers: str,
    topics: list[KafkaTopicSpec],
    request_timeout_s: float = 5.0,
) -> None:
    """Executes best-effort topic creation offloaded to a worker thread to prevent blocking."""
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
    """Small non-blocking JSON producer wrapper using native AIO internal queuing."""

    def __init__(self, cfg: KafkaConnectorConfig) -> None:
        self._cfg = cfg
        self._producer = AIOProducer(
            {"bootstrap.servers": cfg.bootstrap_servers}
        )

    async def produce_json(
        self, *, topic: str, key: str, payload: dict[str, Any]
    ) -> None:
        """Asynchronously triggers message delivery pipeline.

        Awaiting produce() returns immediately once the item enters the internal queue.
        """
        data = orjson.dumps(payload)
        await self._producer.produce(topic=topic, key=key, value=data)

    async def flush(self, timeout_s: float = 10.0) -> None:
        """Flushes the internal buffer buffers within the maximum timeout window."""
        await self._producer.flush(timeout_s)
