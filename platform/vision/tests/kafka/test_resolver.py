"""Unit tests targeting local schema file compilation and remote Avro registry lookups."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

from galadril_vision.connectors.kafka.resolver import DynamicEventResolver


class FakeSourceConfig:
    """Stub configuring mock schema indexing entries."""

    def __init__(self, source_id: str, path: str) -> None:
        self.id = source_id
        self.schema_path = path


@pytest.fixture
def mock_sources() -> list[FakeSourceConfig]:
    """Provides a list of standard event source configurations."""
    return [
        FakeSourceConfig("image_source", "/etc/schemas/image.json"),
        FakeSourceConfig("audio_source", "/etc/schemas/audio.json"),
    ]


@patch("galadril_vision.connectors.kafka.resolver.AsyncSchemaRegistryClient")
def test_resolver_initialization_parsing_logic(
    mock_registry_cls: MagicMock, mock_sources: list[FakeSourceConfig]
) -> None:
    """Verifies index maps construction from disk JSON files or error containment bounds."""
    valid_schema = {
        "type": "record",
        "name": "ImageMessage",
        "namespace": "galadril.vision",
    }
    malformed_schema = "invalid-json-content"

    m_open = mock_open()
    m_open.side_effect = [
        mock_open(read_data=json.dumps(valid_schema)).return_value,
        mock_open(read_data=malformed_schema).return_value,
    ]

    with patch("galadril_vision.connectors.kafka.resolver.open", m_open):
        resolver = DynamicEventResolver(
            sources=mock_sources, schema_registry_url="http://localhost"
        )

    assert (
        resolver.record_name_to_event_type["galadril.vision.ImageMessage"]
        == "image_source"
    )
    assert resolver.record_name_to_event_type["ImageMessage"] == "image_source"


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.resolver.AsyncSchemaRegistryClient")
async def test_resolve_event_type_header_validations(
    mock_registry_cls: MagicMock, mock_sources: list[FakeSourceConfig]
) -> None:
    """Ensures short payloads or missing magic bytes return UNKNOWN directly."""
    with patch(
        "galadril_vision.connectors.kafka.resolver.open",
        mock_open(read_data="{}"),
    ):
        resolver = DynamicEventResolver(
            sources=mock_sources, schema_registry_url="http://localhost"
        )

    assert await resolver.resolve_event_type(b"\x01\x00\x00") == "UNKNOWN"
    assert (
        await resolver.resolve_event_type(b"\x01\x00\x00\x00\x02abc")
        == "UNKNOWN"
    )


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.resolver.AsyncSchemaRegistryClient")
async def test_resolve_event_type_registry_caching_and_extraction(
    mock_registry_cls: MagicMock, mock_sources: list[FakeSourceConfig]
) -> None:
    """Validates successful remote schema lookups, list unpacking, and caching."""
    with patch(
        "galadril_vision.connectors.kafka.resolver.open",
        mock_open(read_data="{}"),
    ):
        resolver = DynamicEventResolver(
            sources=mock_sources, schema_registry_url="http://localhost"
        )

    resolver.schema_id_to_event_type[42] = "cached_source"
    assert (
        await resolver.resolve_event_type(b"\x00\x00\x00\x00\x2acontent")
        == "cached_source"
    )

    resolver._failed_schema_ids.add(99)
    assert (
        await resolver.resolve_event_type(b"\x00\x00\x00\x00\x63content")
        == "UNKNOWN"
    )

    mock_registry = AsyncMock()
    resolver.registry_client = mock_registry

    mock_schema_obj = MagicMock()
    mock_schema_obj.schema_str = json.dumps(
        [
            {
                "type": "record",
                "name": "AudioMessage",
                "namespace": "galadril.vision",
            }
        ]
    )
    mock_registry.get_schema.return_value = mock_schema_obj
    resolver.record_name_to_event_type["galadril.vision.AudioMessage"] = (
        "audio_source"
    )

    res = await resolver.resolve_event_type(b"\x00\x00\x00\x00\x01data")
    assert res == "audio_source"
    assert resolver.schema_id_to_event_type[1] == "audio_source"


@pytest.mark.asyncio
@patch("galadril_vision.connectors.kafka.resolver.AsyncSchemaRegistryClient")
async def test_resolve_event_type_registry_failures(
    mock_registry_cls: MagicMock, mock_sources: list[FakeSourceConfig]
) -> None:
    """Ensures exceptions from the registry block further attempts by registering the schema ID as failed."""
    with patch(
        "galadril_vision.connectors.kafka.resolver.open",
        mock_open(read_data="{}"),
    ):
        resolver = DynamicEventResolver(
            sources=mock_sources, schema_registry_url="http://localhost"
        )

    mock_registry = AsyncMock()
    mock_registry.get_schema.side_effect = Exception(
        "Registry connection error"
    )
    resolver.registry_client = mock_registry

    res = await resolver.resolve_event_type(b"\x00\x00\x00\x00\x05error")
    assert res == "UNKNOWN"
    assert 5 in resolver._failed_schema_ids
