"""PostgreSQL async client with zero-overhead native connection pooling."""

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
    """Async PostgreSQL client leveraging background pooled state initialization."""

    def __init__(self, config: PostgresConnectorConfig) -> None:
        self._config = config
        self._pool: AsyncConnectionPool[AsyncConnection[Any]] | None = None
        self._connect_lock = asyncio.Lock()

    @staticmethod
    async def _configure_pooled_connection(conn: AsyncConnection[Any]) -> None:
        """Asynchronously pre-loads Apache AGE context on connection allocation.

        This eliminates redundant network round-trips from the hot transaction path.
        """
        async with conn.cursor() as cur:
            await cur.execute("LOAD 'age';")
            await cur.execute("SET search_path = public, ag_catalog, '$user';")

    async def connect(
        self, *, initialize_database_infrastructure: bool = True
    ) -> None:
        """Initialize the connection pool with background worker configuration hooks."""
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
                configure=self._configure_pooled_connection,
            )
            await pool.open()
            self._pool = pool

            try:
                if initialize_database_infrastructure:
                    await self._init_database_infrastructure()
            except Exception:
                await pool.close()
                self._pool = None
                raise

            logger.info(
                "postgres_pool_initialized",
                min_size=self._config.min_connections,
                max_size=self._config.max_connections,
            )

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
            extensions = (
                "timescaledb",
                "vector",
                "vectorscale",
                "age",
                "postgis",
                "plpython3u",
                "pg_stat_statements",
                "pg_wait_sampling",
                "pg_repack",
                "pg_trgm",
            )
            for ext in extensions:
                await sa_conn.execute(
                    text(f"CREATE EXTENSION IF NOT EXISTS {ext} CASCADE;")
                )

            await sa_conn.execute(text("LOAD 'age';"))
            await sa_conn.execute(
                text("SET search_path = public, ag_catalog, '$user';")
            )

            graph_name = self._config.graph_name
            result = await sa_conn.execute(
                text(
                    "SELECT 1 FROM ag_catalog.ag_graph WHERE name = :name_str"
                ),
                {"name_str": graph_name},
            )
            if not result.fetchone():
                await sa_conn.execute(
                    text("SELECT * FROM ag_catalog.create_graph(:name)"),
                    {"name": graph_name},
                )

            await sa_conn.run_sync(Base.metadata.create_all)
            await self._ensure_schema_invariants(sa_conn)

        await engine.dispose()
        logger.info(
            "postgres_extensions_and_schema_initialized", graph=graph_name
        )

    async def _ensure_schema_invariants(self, conn: Any) -> None:
        """Repair schema invariants that metadata.create_all() cannot backfill."""
        statements = (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_authz_outbox_tenant_object
            ON authz_outbox (tenant_id, object_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_eskg_events_tenant_event_time
            ON eskg_events (tenant_id, event_id, event_time)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_entity_embeddings_tenant_id_id_created_at
            ON entity_embeddings (tenant_id, id, created_at)
            """,
        )
        for statement in statements:
            await conn.execute(text(statement))

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[Any]]:
        """Get a pre-configured connection from the pool without hot-path setup latency."""
        if self._pool is None:
            raise RuntimeError("Pool not initialized. Call connect() first.")

        async with self._pool.connection() as conn:
            yield conn

    async def close(self) -> None:
        """Close the connection pool cleanly."""
        async with self._connect_lock:
            if self._pool:
                await self._pool.close()
                self._pool = None
                logger.info("postgres_pool_closed")

    async def __aenter__(self) -> "PostgresClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
