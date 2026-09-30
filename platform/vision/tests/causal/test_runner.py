"""Tests for Vision's ESKG-to-Amarth observation bridge."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from galadril_vision.causal.runner import (
    _build_observation_window,
    _cache_get,
    _cache_put,
    _load_embedding_rows,
    _load_event_rows,
    _load_state_rows,
)
from galadril_vision.connectors.postgres.client import PostgresClient


class _Cursor:
    async def fetchone(self) -> None:
        return None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _Connection:
    def __init__(self) -> None:
        self.statements = 0

    async def execute(
        self, query: str, parameters: tuple[object, ...]
    ) -> _Cursor:
        assert query.count("%s") == len(parameters)
        assert "$1" not in query
        self.statements += 1
        return _Cursor()


class _Client:
    def __init__(self) -> None:
        self.connection = _Connection()

    @asynccontextmanager
    async def tenant_connection(
        self, tenant_id: str
    ) -> AsyncIterator[_Connection]:
        assert tenant_id == "tenant-a"
        yield self.connection


def test_causal_queries_use_psycopg_placeholders() -> None:
    """Keeps every causal SQL path compatible with the live psycopg pool."""
    client = _Client()
    pg = cast(PostgresClient, client)
    now = datetime(2026, 9, 30, tzinfo=UTC)

    async def exercise() -> None:
        assert await _cache_get(pg, "tenant-a", "key") is None
        await _cache_put(
            pg,
            tenant_id="tenant-a",
            cache_key="key",
            target="entity:one",
            window_start=now,
            window_end=now,
            status="success",
            result_summary={"causal_links": 1},
        )
        assert (
            await _load_state_rows(
                pg,
                tenant_id="tenant-a",
                entity_ids=("one",),
                window_start=now,
                window_end=now,
                max_rows=30,
            )
            == ()
        )
        assert (
            await _load_embedding_rows(
                pg,
                tenant_id="tenant-a",
                entity_ids=("one",),
                window_start=now,
                window_end=now,
                max_rows=30,
            )
            == ()
        )
        assert (
            await _load_event_rows(
                pg,
                tenant_id="tenant-a",
                event_ids=("event-one",),
                window_start=now,
                window_end=now,
                max_rows=30,
            )
            == ()
        )

    asyncio.run(exercise())
    assert client.connection.statements == 5


def test_build_observation_window_keeps_states_embeddings_and_edges() -> None:
    """Builds one typed window without discarding multimodal graph evidence."""
    start = datetime(2026, 8, 24, tzinfo=UTC)
    window = _build_observation_window(
        window_start=start,
        window_end=start + timedelta(seconds=5),
        bucket_seconds=1.0,
        state_rows=(
            (
                start,
                "FacialExpressionShift",
                {"confidence": 0.91},
                "face-node",
                "face-event",
            ),
        ),
        embedding_rows=(
            (
                start,
                "face",
                [0.1, 0.2, 0.3],
                "face-node",
                {
                    "state_type": "FacialExpressionShift",
                    "event_id": "face-event",
                },
                "embedding-1",
            ),
        ),
        relationship_rows=(
            (
                "face-node",
                "text-node",
                "TRIGGERS",
                {"timestamp": (start + timedelta(seconds=2)).isoformat()},
            ),
        ),
    )

    assert len(window.observations) == 2
    assert window.observations[1].embeddings["face_embedding"] == (
        0.1,
        0.2,
        0.3,
    )
    assert window.relationships[0].relationship_type == "TRIGGERS"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
