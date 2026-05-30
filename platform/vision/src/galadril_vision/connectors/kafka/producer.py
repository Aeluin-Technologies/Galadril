"""Kafka producer utilities (DLQ/retry) with best-effort topic auto-creation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import orjson
import structlog
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient
from confluent_kafka.cimpl import KafkaException, NewTopic

from galadril_vision.common.config import KafkaConfig

logger = structlog.get_logger(__name__)

_DEFAULT_AUTHZ_DLQ_TOPIC = "galadril.authz.dlq"


@dataclass(frozen=True, slots=True)
class KafkaTopicSpec:
    name: str
    partitions: int = 1
    replication_factor: int = 1


def resolve_authz_dlq_topic(cfg: KafkaConfig) -> str:
    """Resolve the authz DLQ topic with a safe hardcoded fallback."""
    t = (cfg.authz_dlq_topic or "").strip()
    return t if t else _DEFAULT_AUTHZ_DLQ_TOPIC


def ensure_topics(
    *,
    bootstrap_servers: str,
    topics: list[KafkaTopicSpec],
    request_timeout_s: float = 5.0,
) -> None:
    """
    Best-effort topic creation.
    """
    if not topics:
        return

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
        fs = admin.create_topics(new_topics, request_timeout=request_timeout_s)
    except Exception as exc:
        logger.warning("kafka_create_topics_request_failed", error=str(exc))
        return

    for name, fut in fs.items():
        try:
            fut.result()
            logger.info("kafka_topic_created", topic=name)
        except KafkaException as exc:
            # TopicAlreadyExists is not an error for us.
            msg = str(exc)
            if "TOPIC_ALREADY_EXISTS" in msg or "TopicAlreadyExists" in msg:
                continue
            logger.warning("kafka_topic_create_failed", topic=name, error=msg)
        except Exception as exc:
            logger.warning(
                "kafka_topic_create_failed", topic=name, error=str(exc)
            )


class KafkaJsonProducer:
    """Small JSON producer wrapper for DLQ-style messages."""

    def __init__(self, cfg: KafkaConfig) -> None:
        self._cfg = cfg
        self._producer = Producer({"bootstrap.servers": cfg.bootstrap_servers})

    def produce_json(
        self, *, topic: str, key: str, payload: dict[str, Any]
    ) -> None:
        """
        Fire-and-forget production; caller can poll/flush if they need delivery guarantees.
        """
        data = orjson.dumps(payload)
        self._producer.produce(topic=topic, key=key, value=data)

    def poll(self, timeout_s: float = 0.0) -> None:
        self._producer.poll(timeout_s)

    def flush(self, timeout_s: float = 10.0) -> None:
        self._producer.flush(timeout_s)
