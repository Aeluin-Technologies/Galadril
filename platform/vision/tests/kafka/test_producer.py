"""Unit tests for the FastStream JSON publisher adapter."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from galadril_vision.common.config import KafkaConnectorConfig
from galadril_vision.connectors.kafka.producer import (
    KafkaJsonProducer,
    resolve_authz_dlq_topic,
)


def test_resolve_authz_dlq_topic_fallbacks() -> None:
    """Resolves custom and default authorization DLQ topics."""
    config = MagicMock(spec=KafkaConnectorConfig)
    config.authz_dlq_topic = "custom.authz.dlq"
    assert resolve_authz_dlq_topic(config) == "custom.authz.dlq"

    config.authz_dlq_topic = "   "
    assert resolve_authz_dlq_topic(config) == "galadril.authz.dlq"


@pytest.mark.anyio
async def test_json_producer_uses_confirmed_faststream_publish() -> None:
    """Keeps publishing inside FastStream tracing and delivery confirmation."""
    broker = MagicMock()
    broker.publish = AsyncMock()
    producer = KafkaJsonProducer(broker)

    await producer.produce_json(
        topic="authz.dlq", key="tenant:object", payload={"attempt": 3}
    )

    broker.publish.assert_awaited_once_with(
        {"attempt": 3},
        "authz.dlq",
        key="tenant:object",
        no_confirm=False,
    )


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
