"""Dynamic event type resolver based on Avro schemas."""

from __future__ import annotations

import json
import struct
from typing import Any

import structlog
from confluent_kafka.schema_registry import SchemaRegistryClient

logger = structlog.get_logger(__name__)


class DynamicEventResolver:
    """Resolves raw Kafka message payloads to dynamic event types using Schema Registry."""

    def __init__(self, sources: list[Any], schema_registry_url: str) -> None:
        """Initializes the resolver by parsing local schemas and indexing record names.

        Args:
            sources: A list of SourceConfig instances defining local source metadata.
            schema_registry_url: The endpoint URL of the Redpanda/Kafka Schema Registry.
        """
        self.registry_client = SchemaRegistryClient(
            {"url": schema_registry_url}
        )
        self.schema_id_to_event_type: dict[int, str] = {}
        self.record_name_to_event_type: dict[str, str] = {}
        self._failed_schema_ids: set[int] = set()

        # Pre-index local schemas to build a manifest matching Avro Record
        # names to source IDs.
        for source in sources:
            try:
                with open(source.schema_path, "r") as f:
                    schema_data = json.load(f)

                if (
                    isinstance(schema_data, dict)
                    and schema_data.get("type") == "record"
                ):
                    name = schema_data.get("name")
                    namespace = schema_data.get("namespace")

                    full_name = f"{namespace}.{name}" if namespace else name
                    if full_name:
                        self.record_name_to_event_type[full_name] = source.id
                    if name:
                        self.record_name_to_event_type[name] = source.id

                logger.info(
                    "schema_indexed",
                    path=source.schema_path,
                    source_id=source.id,
                )
            except Exception as exc:
                logger.error(
                    "failed_to_parse_local_schema",
                    path=source.schema_path,
                    error=str(exc),
                )

    def resolve_event_type(self, raw_bytes: bytes) -> str:
        """Extracts the schema ID from the Confluent Wire Format header and matches the source ID.

        Confluent Avro wire format structure:
        - Byte 0: Magic byte (0x00)
        - Bytes 1-4: 4-byte Schema ID (Big-Endian integer)

        Args:
            raw_bytes: Raw binary payload received from the Kafka topic.

        Returns:
            The resolved string event type identifier, or "UNKNOWN" if resolution fails.
        """
        if len(raw_bytes) < 5 or raw_bytes[0] != 0:
            logger.warning(
                "schema_resolution_skipped_invalid_header",
                length=len(raw_bytes),
                magic_byte=raw_bytes[0] if len(raw_bytes) > 0 else None,
            )
            return "UNKNOWN"

        # Unpack 4-byte big-endian integer starting at index 1.
        schema_id = struct.unpack(">I", raw_bytes[1:5])[0]

        if schema_id in self.schema_id_to_event_type:
            return self.schema_id_to_event_type[schema_id]

        if schema_id in self._failed_schema_ids:
            return "UNKNOWN"

        try:
            schema_obj = self.registry_client.get_schema(schema_id)

            if not schema_obj or not schema_obj.schema_str:
                logger.error(
                    "schema_content_is_empty_or_null", schema_id=schema_id
                )
                self._failed_schema_ids.add(schema_id)
                return "UNKNOWN"

            schema_json = json.loads(schema_obj.schema_str)

            name = schema_json.get("name")
            namespace = schema_json.get("namespace")
            full_name = f"{namespace}.{name}" if namespace else name

            event_type = self.record_name_to_event_type.get(
                full_name, self.record_name_to_event_type.get(name, "UNKNOWN")
            )

            self.schema_id_to_event_type[schema_id] = event_type

            logger.info(
                "schema_resolved_successfully",
                schema_id=schema_id,
                avro_record=full_name,
                event_type=event_type,
            )
            return event_type

        except Exception as exc:
            logger.error(
                "failed_dynamic_schema_resolution",
                schema_id=schema_id,
                error=str(exc),
            )
            self._failed_schema_ids.add(schema_id)
            return "UNKNOWN"
