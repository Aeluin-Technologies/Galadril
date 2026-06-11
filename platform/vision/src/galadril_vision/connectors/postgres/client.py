"""PostgreSQL client."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, Any, cast

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from galadril_vision.connectors.postgres.models import Base

if TYPE_CHECKING:
    from galadril_vision.common.config import PostgresConnectorConfig

logger = structlog.get_logger(__name__)


class PostgresClient:
    """Async PostgreSQL client with connection pooling."""

    def __init__(self, config: PostgresConnectorConfig) -> None:
        self._config = config
        self._pool: AsyncConnectionPool[AsyncConnection[Any]] | None = None
        self._connect_lock = asyncio.Lock()
        self._session_ready = False

    async def connect(self) -> None:
        """Initialize the connection pool."""
        if self._pool is not None:
            return

        async with self._connect_lock:
            if self._pool is not None:
                return

            pool = AsyncConnectionPool[AsyncConnection[Any]](
                conninfo=str(self._config.dsn),
                min_size=self._config.min_connections,
                max_size=self._config.max_connections,
                open=False,
            )
            await pool.open()
            self._pool = pool

            try:
                await self._init_database_infrastructure()
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

    async def _prepare_session(self, conn: AsyncConnection[Any]) -> None:
        """Load connection-local AGE state and deterministic search paths."""
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public, '$user';")

    async def _init_database_infrastructure(self) -> None:
        """Ensure required PostgreSQL extensions are loaded and optimized."""
        sa_dsn = str(self._config.dsn).replace(
            "postgresql://", "postgresql+psycopg://"
        )

        for column in Base.metadata.tables["entity_embeddings"].columns:
            if column.name == "embedding":
                if hasattr(column.type, "dimensions"):
                    cast(Any, column.type).dimensions = int(
                        self._config.vector_dimensions
                    )

        engine = create_async_engine(sa_dsn)

        async with engine.begin() as sa_conn:
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
            )
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS vector CASCADE;")
            )
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;")
            )
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS age CASCADE;")
            )
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS postgis CASCADE;")
            )
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS plpython3u CASCADE;")
            )
            await sa_conn.execute(
                text(
                    "CREATE EXTENSION IF NOT EXISTS pg_stat_statements CASCADE;"
                )
            )
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS pg_wait_sampling CASCADE;")
            )
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS pg_repack CASCADE;")
            )
            await sa_conn.execute(
                text("CREATE EXTENSION IF NOT EXISTS pg_trgm CASCADE;")
            )

            await sa_conn.execute(text("LOAD 'age';"))
            await sa_conn.execute(
                text("SET search_path = ag_catalog, public, '$user';")
            )

            graph_name = self._config.graph_name
            await sa_conn.execute(
                text("""
                    SELECT * FROM ag_catalog.create_graph(:name)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM ag_catalog.ag_graph WHERE name = :name_str
                    )
                """),
                {"name": graph_name, "name_str": graph_name},
            )

            await sa_conn.run_sync(Base.metadata.create_all)

        await engine.dispose()

        logger.info(
            "postgres_extensions_and_schema_initialized", graph=graph_name
        )

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[Any]]:
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
