"""Official email capabilities must be available without importing clients."""

import json
import unittest
from pathlib import Path

from galadril_plugins import PluginRegistry


class Packaging(unittest.TestCase):
    def test_email_schema_preserves_native_routing_metadata(self) -> None:
        schema = json.loads(
            (
                Path(__file__).resolve().parents[2] / "schemas/avro/email.avsc"
            ).read_text()
        )
        fields = {item["name"]: item["type"] for item in schema["fields"]}
        self.assertEqual(fields["content"], "string")
        reference = next(
            field
            for field in fields["metadata"]["fields"]
            if field["name"] == "reference"
        )
        self.assertEqual(reference["type"], "string")
        self.assertEqual(reference["default"], "")
        names = {item["name"] for item in fields["metadata"]["fields"]}
        self.assertTrue(
            {"account_id", "sender", "subject", "date", "attachments"}.issubset(
                names
            )
        )

    def test_official_email_service_and_skill_are_discovered(self) -> None:
        registry = PluginRegistry.discover()
        skill = next(
            item
            for item in registry.catalog.capabilities
            if item.id == "utils.skill.email"
        )
        self.assertIn("utils.mcp.email", skill.compatibility.requires)
        self.assertIn(
            "utils.input.email",
            {item.id for item in registry.catalog.capabilities},
        )
        self.assertIn(
            "utils.skill.email",
            {item.capability.id for item in registry.skills()},
        )
        self.assertTrue(
            any(
                plugin == "utils" and service == "email"
                for plugin, service in registry.services()
            )
        )


if __name__ == "__main__":
    unittest.main()
