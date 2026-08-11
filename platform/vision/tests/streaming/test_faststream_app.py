"""Unit tests for FastStream application wiring and Avro decoding."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from confluent_kafka.schema_registry import AsyncSchemaRegistryClient
from faststream.message import StreamMessage
from galadril_vision.common.config import VisionConfig
from galadril_vision.streaming.app import (
    ServiceRole,
    _initialize_ray,
    build_stream_app,
    faststream_logger,
)
from galadril_vision.streaming.codec import AvroMessageDecoder


class _Registry:
    """Records Schema Registry lifecycle calls for the decoder."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Resolver:
    """Resolves every test schema to one configured source."""

    def __init__(self) -> None:
        self.registry = _Registry()
        self.registry_client = cast(AsyncSchemaRegistryClient, self.registry)

    async def resolve_event_type(self, raw_bytes: bytes) -> str:
        assert raw_bytes == b"avro-wire"
        return "image_source"

    async def close(self) -> None:
        await self.registry.close()


class _Deserializer:
    """Returns an Avro logical timestamp to exercise JSON normalization."""

    async def __call__(self, data: bytes, context: object) -> dict[str, object]:
        del context
        assert data == b"avro-wire"
        return {
            "id": "record-1",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }


class _RawMessage:
    """Provides the raw topic method used by the Confluent decoder."""

    def topic(self) -> str:
        return "raw"


async def _deserializer_factory(
    client: AsyncSchemaRegistryClient,
) -> _Deserializer:
    assert client is not None
    return _Deserializer()


def _config() -> VisionConfig:
    """Creates a minimal application configuration with real schema paths."""
    return VisionConfig.model_validate(
        {
            "name": "vision",
            "connectors": {
                "kafka": {
                    "brokers": ["localhost:9092"],
                    "schema_registry": "http://localhost:8081",
                    "consumer_group": "test",
                },
                "s3": {
                    "endpoint": "http://localhost:9000",
                    "access_key": "test",
                    "secret_key": "test",
                    "region": "us-east-1",
                    "bucket": "raw",
                },
                "postgres": {
                    "database": "test",
                    "host": "localhost:5432",
                    "user": "test",
                    "password": "test",
                },
                "spicedb": {
                    "endpoint": "localhost:50051",
                    "token": "test",
                },
            },
            "sources": [
                {
                    "id": "image_source",
                    "topic": "raw",
                    "match_pattern": ".*",
                    "schema_path": "schemas/avro/image.avsc",
                }
            ],
            "pipeline": [
                {
                    "step": "infer",
                    "type": "inference",
                    "model": "models.Face",
                    "input_from": ["image_source"],
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_avro_decoder_normalizes_logical_types_once() -> None:
    """Ensures decoded Avro enters Pydantic as a plain JSON contract."""
    resolver = _Resolver()
    decoder = AvroMessageDecoder(
        sources=[],
        schema_registry_url="http://unused",
        resolver=resolver,
        deserializer_factory=_deserializer_factory,
    )
    message = StreamMessage(
        raw_message=_RawMessage(),
        body=b"avro-wire",
    )

    decoded = await decoder(message)
    await decoder.close()

    assert decoded["source_id"] == "image_source"
    assert decoded["payload"]["timestamp"] == "2026-01-01T00:00:00+00:00"
    assert resolver.registry.closed is True


def test_app_registers_role_specific_non_batch_subscribers() -> None:
    """Separates ingress and GPU deployments on the same application factory."""
    ingress = build_stream_app(
        _config(),
        role=ServiceRole.INGRESS,
    )
    gpu = build_stream_app(
        _config(),
        role=ServiceRole.GPU,
    )

    assert ingress.broker is not None
    assert gpu.broker is not None
    assert len(ingress.broker._subscribers) == 1
    assert len(gpu.broker._subscribers) == 1
    assert ingress.logger is faststream_logger
    assert isinstance(ingress.logger, logging.Logger)


def test_ray_initializes_process_local_runtime_without_dashboard() -> None:
    """Prevents Compose deployments from reconnecting to a Ray cluster."""
    ray_module = MagicMock()
    ray_module.is_initialized.return_value = False

    with patch.dict(sys.modules, {"ray": ray_module}):
        started = _initialize_ray(_config())

    assert started is True
    ray_module.init.assert_called_once_with(
        address=None,
        num_cpus=None,
        num_gpus=None,
        namespace="galadril",
        include_dashboard=False,
        ignore_reinit_error=True,
        log_to_driver=False,
    )
