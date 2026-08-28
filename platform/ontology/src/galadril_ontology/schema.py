"""Loads idempotent PostgreSQL resources owned by Ontology."""

from __future__ import annotations

from importlib.resources import files


def postgres_schema_sql() -> tuple[str, ...]:
    """Returns shared extension, table, RLS, and trigger SQL in order."""
    root = files("galadril_ontology").joinpath("sql")
    return tuple(
        resource.read_text(encoding="utf-8")
        for resource in sorted(root.iterdir(), key=lambda item: item.name)
        if resource.is_file() and resource.name.endswith(".sql")
    )
