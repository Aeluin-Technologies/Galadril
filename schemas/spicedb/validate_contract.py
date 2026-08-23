"""Validates the canonical SpiceDB permission contract."""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.zed")
README = Path(__file__).with_name("README.md")
REQUIRED_RESOURCES = frozenset(
    {
        "tenant",
        "raw",
        "document",
        "ontology",
        "pipeline",
        "entity_state",
        "event",
    }
)
PERMISSION_RE = re.compile(
    r"^\s*permission\s+([a-z][a-z0-9_]*)\s*=", re.MULTILINE
)
DEFINITION_RE = re.compile(
    r"^definition\s+([a-z][a-z0-9_]*)\s*\{", re.MULTILINE
)


def validate(schema: str, documentation: str) -> None:
    """Rejects missing roots, duplicate definitions, and unstable permission names."""
    definitions = DEFINITION_RE.findall(schema)
    if len(definitions) != len(set(definitions)):
        raise ValueError("duplicate SpiceDB definition")
    missing = REQUIRED_RESOURCES.difference(definitions)
    if missing:
        raise ValueError(f"missing required definitions: {sorted(missing)}")
    permissions = PERMISSION_RE.findall(schema)
    if not permissions:
        raise ValueError("schema contains no permissions")
    forbidden = {name for name in permissions if name.startswith("can_")}
    if forbidden:
        raise ValueError(
            f"implementation-shaped permission names: {sorted(forbidden)}"
        )
    undocumented = {
        name for name in permissions if f"`{name}`" not in documentation
    }
    if undocumented:
        raise ValueError(f"undocumented permissions: {sorted(undocumented)}")


if __name__ == "__main__":
    validate(
        SCHEMA.read_text(encoding="utf-8"),
        README.read_text(encoding="utf-8"),
    )
