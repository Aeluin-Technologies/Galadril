"""PostgreSQL client."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

import structlog
from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool

if TYPE_CHECKING:
    from galadril_vision.common.config import PostgresConfig

logger = structlog.get_logger(__name__)

_CAUSAL_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS causal_runs (
    cache_key      TEXT PRIMARY KEY,
    target         TEXT NOT NULL,
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status         TEXT NOT NULL,
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_causal_runs_window
ON causal_runs (window_start DESC, window_end DESC);

CREATE INDEX IF NOT EXISTS idx_causal_runs_target
ON causal_runs (target, created_at DESC);
"""

_AUTHZ_OUTBOX_SQL = """
CREATE TABLE IF NOT EXISTS authz_outbox (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL CHECK (tenant_id <> ''),
    object_id     TEXT NOT NULL CHECK (object_id <> ''),
    tuples_json   JSONB NOT NULL CHECK (jsonb_typeof(tuples_json) = 'array'),
    attempts      INT NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_authz_outbox_tenant_object
ON authz_outbox (tenant_id, object_id);

CREATE INDEX IF NOT EXISTS idx_authz_outbox_retry
ON authz_outbox (next_retry_at ASC);
"""

_AUTHZ_OUTBOX_CONSTRAINTS_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_authz_outbox_tenant_nonempty'
    ) THEN
        ALTER TABLE authz_outbox
        ADD CONSTRAINT ck_authz_outbox_tenant_nonempty
        CHECK (tenant_id <> '') NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_authz_outbox_object_nonempty'
    ) THEN
        ALTER TABLE authz_outbox
        ADD CONSTRAINT ck_authz_outbox_object_nonempty
        CHECK (object_id <> '') NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_authz_outbox_tuples_array'
    ) THEN
        ALTER TABLE authz_outbox
        ADD CONSTRAINT ck_authz_outbox_tuples_array
        CHECK (jsonb_typeof(tuples_json) = 'array') NOT VALID;
    END IF;
END $$;
"""


class PostgresClient:
    """Async PostgreSQL client with connection pooling."""

    def __init__(self, config: PostgresConfig) -> None:
        self._config = config
        self._pool: AsyncConnectionPool | None = None
        self._connect_lock = asyncio.Lock()
        self._session_ready = False

    async def connect(self) -> None:
        """Initialize the connection pool."""
        if self._pool is not None:
            return

        async with self._connect_lock:
            if self._pool is not None:
                return

            pool = AsyncConnectionPool(
                conninfo=str(self._config.dsn),
                min_size=self._config.min_connections,
                max_size=self._config.max_connections,
                open=False,
            )
            await pool.open()
            self._pool = pool

            try:
                async with self.connection() as conn:
                    await self._init_extensions(conn)
                self._session_ready = True
            except Exception:
                await pool.close()
                self._pool = None
                self._session_ready = False
                raise

            logger.info(
                "postgres_pool_initialized",
                min_size=self._config.min_connections,
                max_size=self._config.max_connections,
            )

    async def _prepare_session(self, conn: AsyncConnection) -> None:
        """Load connection-local AGE state and deterministic search paths."""
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public, '$user';")

    async def _init_extensions(self, conn: AsyncConnection) -> None:
        """Ensure required PostgreSQL extensions are loaded and optimized."""
        await conn.execute(
            "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"
        )
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector CASCADE;")
        await conn.execute(
            "CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;"
        )
        await conn.execute("CREATE EXTENSION IF NOT EXISTS age CASCADE;")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis CASCADE;")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS plpython3u CASCADE;")
        await conn.execute(
            "CREATE EXTENSION IF NOT EXISTS pg_stat_statements CASCADE;"
        )
        await conn.execute(
            "CREATE EXTENSION IF NOT EXISTS pg_wait_sampling CASCADE;"
        )
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_repack CASCADE;")

        await self._prepare_session(conn)

        graph_name = self._config.graph_name
        query = sql.SQL("""
            SELECT * FROM ag_catalog.create_graph({name})
            WHERE NOT EXISTS (
                SELECT 1 FROM ag_catalog.ag_graph WHERE name = {name_str}
            )
        """).format(
            name=sql.Literal(graph_name),
            name_str=sql.Literal(graph_name),
        )

        await conn.execute(query)
        await conn.execute(_CAUSAL_RUNS_SQL)
        await conn.execute(_AUTHZ_OUTBOX_SQL)
        await conn.execute(_AUTHZ_OUTBOX_CONSTRAINTS_SQL)

        logger.info("postgres_extensions_initialized", graph=graph_name)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """Get a connection from the pool."""
        if self._pool is None:
            raise RuntimeError("Pool not initialized. Call connect() first.")

        async with self._pool.connection() as conn:
            if self._session_ready:
                await self._prepare_session(conn)
            yield conn

    async def close(self) -> None:
        """Close the connection pool."""
        async with self._connect_lock:
            if self._pool:
                await self._pool.close()
                self._pool = None
                self._session_ready = False
                logger.info("postgres_pool_closed")

    async def __aenter__(self) -> "PostgresClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
