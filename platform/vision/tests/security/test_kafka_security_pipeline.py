"""Kafka boundary tests for the Intake-to-Vision security envelope."""

from __future__ import annotations

import json

import pytest
from confluent_kafka import Consumer, Producer
from galadril_vision.connectors.kafka.schemas import EventNormalizer

KafkaContainer = pytest.importorskip("testcontainers.kafka").KafkaContainer

KAFKA_IMAGE = "confluentinc/cp-kafka:7.6.0"
TOPIC = "security-ingestion"


def _payload(tenant_id: str, authz_tenant: str) -> dict[str, object]:
    resource = f"raw:{authz_tenant}/image/object-1"
    return {
        "id": "object-1",
        "tenant_id": tenant_id,
        "timestamp": 1_774_785_600_000,
        "ingested_at": 1_774_785_600_000,
        "source": "intake",
        "authz": {
            "tenant_id": authz_tenant,
            "source_principal": "service:intake",
            "execution_identity": "service:intake",
            "authentication_provenance": "https://issuer.example",
            "delegation_id": "delegation-1",
            "requested_permission": "materialize",
            "requested_resource": resource,
            "tuples": [
                {
                    "resource": resource,
                    "relation": "parent",
                    "subject": f"tenant:{authz_tenant}",
                }
            ],
        },
    }


def _consume(bootstrap: str, expected: int) -> list[dict[str, object]]:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": "security-contract",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([TOPIC])
    records: list[dict[str, object]] = []
    try:
        while len(records) < expected:
            message = consumer.poll(10.0)
            if message is None:
                raise AssertionError("Kafka fixture was not delivered")
            if message.error() is not None:
                raise AssertionError(str(message.error()))
            value = message.value()
            if not isinstance(value, bytes):
                raise AssertionError("Kafka fixture is not bytes")
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise AssertionError("Kafka fixture is not an object")
            records.append(parsed)
    finally:
        consumer.close()
    return records


def test_kafka_preserves_valid_context_and_rejects_forged_tenant() -> None:
    """Publishes deterministic envelopes and applies Vision's real validator."""
    with KafkaContainer(KAFKA_IMAGE).with_kraft() as kafka:
        bootstrap = kafka.get_bootstrap_server()
        producer = Producer({"bootstrap.servers": bootstrap})
        producer.produce(
            TOPIC,
            json.dumps(_payload("tenant-a", "tenant-a")),
            key="object-1",
        )
        producer.produce(
            TOPIC,
            json.dumps(_payload("tenant-a", "tenant-b")),
            key="object-1",
        )
        undelivered = producer.flush(20.0)
        assert undelivered == 0

        valid, forged = _consume(bootstrap, 2)
        normalized = EventNormalizer.normalize(valid, "image_source")
        assert normalized["tenant_id"] == "tenant-a"
        with pytest.raises(ValueError, match="tenant_id mismatch"):
            EventNormalizer.normalize(forged, "image_source")


if __name__ == "__main__":
    test_kafka_preserves_valid_context_and_rejects_forged_tenant()
