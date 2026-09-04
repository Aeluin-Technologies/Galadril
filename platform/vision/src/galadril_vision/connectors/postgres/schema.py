"""Loads idempotent operational PostgreSQL resources owned by Vision."""

from __future__ import annotations

from importlib.resources import files


def vision_schema_sql() -> tuple[str, ...]:
    """Returns Vision extension and security SQL in deterministic order."""
    root = files("galadril_vision.connectors.postgres").joinpath("sql")
    return tuple(
        resource.read_text(encoding="utf-8")
        for resource in sorted(root.iterdir(), key=lambda item: item.name)
        if resource.is_file() and resource.name.endswith(".sql")
    )
