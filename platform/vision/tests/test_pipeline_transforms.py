"""Unit tests for synchronous Daft UDF helpers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock, patch

from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import (
    GraphStore,
    _cypher_identifier,
)
from galadril_vision.pipeline import transforms


class _Series:
    """Small Series stand-in exposing the Daft method used by wrapped UDFs."""

    __slots__ = ("_items",)

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def to_pylist(self) -> list[Any]:
        """Return the in-memory values without copying test payloads."""
        return self._items


class _Transaction:
    """Async transaction context manager used by the fake PostgreSQL client."""

    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Connection:
    """Fake PostgreSQL connection recording direct SQL executions."""

    __slots__ = ("executed",)

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Transaction:
        """Return a no-op async transaction."""
        return _Transaction()

    async def execute(self, query: str, params: tuple[Any, ...]) -> None:
        """Record SQL and bound parameters."""
        self.executed.append((query, params))


class _CommandConnection:
    """Fake connection recording session setup commands."""

    __slots__ = ("commands",)

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def execute(self, query: str) -> None:
        """Record command text."""
        self.commands.append(query)


class _PostgresClient:
    """Fake PostgreSQL client exposing the async connection context."""

    __slots__ = ("conn",)

    def __init__(self, conn: _Connection) -> None:
        self.conn = conn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        """Yield the fake connection."""
        yield self.conn


def test_run_async_blocking_executes_coroutine_on_running_loop() -> None:
    """Verify synchronous UDF code cannot deadlock on an inactive event loop."""

    async def _value() -> int:
        return 42

    assert transforms._run_async_blocking(_value()) == 42
    assert transforms._get_or_create_loop().is_running()


def test_postgres_search_path_keeps_public_before_age() -> None:
    """Verify SQL tables resolve to public while AGE functions stay available."""

    async def _run() -> list[str]:
        conn = _CommandConnection()
        config = type("Config", (), {"graph_name": "galadril_dev"})()

        client = PostgresClient.__new__(PostgresClient)
        graph_store = GraphStore(client, cast(Any, config))

        await PostgresClient._prepare_session(client, cast(Any, conn))
        await graph_store.prepare_connection(cast(Any, conn))
        return conn.commands

    assert asyncio.run(_run()) == [
        "LOAD 'age';",
        "SET search_path = public, ag_catalog, '$user';",
        "LOAD 'age'",
        "SET search_path = public, ag_catalog, '$user'",
    ]


def test_cypher_identifier_accepts_age_labels_without_sql_quotes() -> None:
    """Verify AGE labels and relationship types are raw validated Cypher tokens."""
    assert _cypher_identifier("Observation") == "Observation"
    assert _cypher_identifier("APPEARS_IN") == "APPEARS_IN"


def test_cypher_identifier_rejects_unsafe_tokens() -> None:
    """Verify unsafe Cypher tokens fail before query interpolation."""
    try:
        _cypher_identifier('Observation") DETACH DELETE v //')
    except Exception as exc:
        assert "invalid Cypher identifier" in str(exc)
    else:
        raise AssertionError("unsafe Cypher identifier was accepted")


def test_sink_to_db_udf_uses_transactional_postgres_methods() -> None:
    """Verify sink writes graph, vectors, states, and outbox on one connection."""
    conn = _Connection()
    pg_client = _PostgresClient(conn)
    graph_store = AsyncMock()
    vector_store = AsyncMock()

    async def _stores(_: object) -> tuple[Any, Any, Any]:
        return pg_client, vector_store, graph_store

    resolved_items = [
        [
            {
                "resolved_entity_id": "person-1",
                "is_unknown": False,
                "confidence": 0.91,
                "bbox": [1, 2, 3, 4],
                "embedding": [0.1, 0.2, 0.3, 0.4],
            }
        ]
    ]

    with patch.object(transforms, "_get_pg_stores", side_effect=_stores):
        result = cast(Any, transforms.sink_to_db_udf).__wrapped__(
            _Series(resolved_items),
            _Series(["record-1"]),
            _Series(["image_source"]),
            _Series(["tenant-a"]),
            _Series(["image_source"]),
            _Series([{"authz": {"tuples": [{"object": "person-1"}]}}]),
            postgres_config=object(),
            entity_type="PERSON",
            modality="face",
        )

    assert result == [True]
    graph_store.insert_event_on_connection.assert_awaited_once()
    graph_store.ensure_vertex_on_connection.assert_awaited_once()
    graph_store.create_edge_on_connection.assert_awaited_once()
    graph_store.insert_entity_states_batch_on_connection.assert_awaited_once()
    vector_store.store_embeddings_batch_on_connection.assert_awaited_once()

    assert len(conn.executed) == 1
    query, params = conn.executed[0]
    assert "INSERT INTO authz_outbox" in query
    assert "%s" in query
    assert "$1" not in query
    assert "ON CONFLICT (tenant_id, object_id)" in query
    assert params[0] == "tenant-a"
