"""Unit tests for the PostgreSQL client bootstrap logic."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.sql.elements import TextClause

from galadril_vision.common.config import PostgresConnectorConfig
from galadril_vision.connectors.postgres.client import PostgresClient


class _FakeAsyncConnection:
    """Collects SQL statements executed during schema repair."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, params: Any = None) -> None:
        self.statements.append((statement, params))


def test_ensure_schema_invariants_creates_authz_outbox_unique_index() -> None:
    """Ensures existing schemas receive the unique index required by ON CONFLICT."""

    async def _run() -> _FakeAsyncConnection:
        config = PostgresConnectorConfig(
            database="galadril",
            host="postgres",
            user="galadril",
            password="galadril",
        )
        client = PostgresClient(config)
        conn = _FakeAsyncConnection()

        await client._ensure_schema_invariants(conn)
        return conn

    conn = asyncio.run(_run())

    assert len(conn.statements) == 3
    statements = [str(statement) for statement, _ in conn.statements]
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_authz_outbox_tenant_object"
        in statement
        for statement in statements
    )
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_eskg_events_tenant_event_time"
        in statement
        for statement in statements
    )
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_entity_embeddings_tenant_id_id_created_at"
        in statement
        for statement in statements
    )
    assert all(params is None for _, params in conn.statements)
