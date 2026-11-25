"""Unit tests for the FastStream JSON publisher adapter."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_ontology import GolDenied
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

    @asynccontextmanager
    async def scope(**kwargs: object) -> AsyncIterator[None]:
        yield

    with patch(
        "galadril_vision.connectors.kafka.producer.protected_gol_state_effect",
        side_effect=scope,
    ):
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


@pytest.mark.anyio
async def test_authz_dead_letter_without_provenance_discloses_no_payload() -> (
    None
):
    """A background maintenance task cannot reset a source's IFC restrictions."""
    broker = MagicMock()
    broker.publish = AsyncMock()
    producer = KafkaJsonProducer(broker)
    with pytest.raises(GolDenied):
        await producer.produce_json(
            topic="authz.dlq",
            key="tenant:protected-object",
            payload={
                "tuples": [{"subject": "private-principal"}],
                "error": "private-value",
            },
        )
    broker.publish.assert_not_awaited()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
