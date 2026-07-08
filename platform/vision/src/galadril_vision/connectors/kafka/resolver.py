"""Schema-based event type resolver."""

from __future__ import annotations

import json
import struct
from typing import Any

import structlog
from confluent_kafka.schema_registry import AsyncSchemaRegistryClient

logger = structlog.get_logger(__name__)


class DynamicEventResolver:
    """Maps raw payloads to system event types using local files and Schema Registry definitions."""

    def __init__(self, sources: list[Any], schema_registry_url: str) -> None:
        """Initializes mappings by parsing local schema definitions.

        Args:
            sources: List of configurations containing schema file paths.
            schema_registry_url: Remote endpoint for the Schema Registry.
        """
        self.registry_client = AsyncSchemaRegistryClient(
            {"url": schema_registry_url}
        )
        self.schema_id_to_event_type: dict[int, str] = {}
        self.record_name_to_event_type: dict[str, str] = {}
        self._failed_schema_ids: set[int] = set()

        for source in sources:
            try:
                with open(source.schema_path) as f:
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

    async def resolve_event_type(self, raw_bytes: bytes) -> str:
        """Identifies the application source ID from the payload's wire-format schema ID.

        Args:
            raw_bytes: Message payload containing the Confluent magic byte header.

        Returns:
            The matched source type identifier, or "UNKNOWN" if lookup fails.
        """
        if len(raw_bytes) < 5 or raw_bytes[0] != 0:
            logger.warning(
                "schema_resolution_skipped_invalid_header",
                length=len(raw_bytes),
                magic_byte=raw_bytes[0] if len(raw_bytes) > 0 else None,
            )
            return "UNKNOWN"

        schema_id = struct.unpack(">I", raw_bytes[1:5])[0]

        if schema_id in self.schema_id_to_event_type:
            return self.schema_id_to_event_type[schema_id]

        if schema_id in self._failed_schema_ids:
            return "UNKNOWN"

        try:
            schema_obj = await self.registry_client.get_schema(schema_id)

            if not schema_obj or not schema_obj.schema_str:
                logger.error(
                    "schema_content_is_empty_or_null", schema_id=schema_id
                )
                self._failed_schema_ids.add(schema_id)
                return "UNKNOWN"

            schema_json = json.loads(schema_obj.schema_str)

            if isinstance(schema_json, list):
                records = [
                    item
                    for item in schema_json
                    if isinstance(item, dict) and item.get("type") == "record"
                ]
                matched_record = None
                for item in records:
                    n = item.get("name")
                    ns = item.get("namespace")
                    fn = f"{ns}.{n}" if ns else n
                    if (
                        isinstance(fn, str)
                        and fn in self.record_name_to_event_type
                        or isinstance(n, str)
                        and n in self.record_name_to_event_type
                    ):
                        matched_record = item
                        break
                schema_json = (
                    matched_record
                    if matched_record
                    else (records[0] if records else {})
                )

            name = None
            namespace = None
            if isinstance(schema_json, dict):
                name_val = schema_json.get("name")
                if isinstance(name_val, str):
                    name = name_val
                namespace_val = schema_json.get("namespace")
                if isinstance(namespace_val, str):
                    namespace = namespace_val

            full_name = f"{namespace}.{name}" if namespace and name else name

            event_type = "UNKNOWN"
            if full_name and full_name in self.record_name_to_event_type:
                event_type = self.record_name_to_event_type[full_name]
            elif name and name in self.record_name_to_event_type:
                event_type = self.record_name_to_event_type[name]

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
