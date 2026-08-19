"""PostgreSQL async connection pool client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

if TYPE_CHECKING:
    from galadril_vision.common.config import PostgresConnectorConfig

logger = structlog.get_logger(__name__)


class PostgresClient:
    """Manages an asynchronous connection pool to PostgreSQL."""

    def __init__(self, config: PostgresConnectorConfig) -> None:
        """Initializes the client.

        Args:
            config: Database connection and configuration settings.
        """
        self._config = config
        self._pool: AsyncConnectionPool[AsyncConnection[Any]] | None = None
        self._connect_lock = asyncio.Lock()

    async def _configure_pooled_connection(
        self, conn: AsyncConnection[Any]
    ) -> None:
        """Initializes unprivileged runtime session configuration."""
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, "
                "has_database_privilege(current_user, current_database(), 'CREATE'), "
                "has_schema_privilege(current_user, 'public', 'CREATE'), "
                "has_schema_privilege(current_user, %s, 'CREATE') "
                "FROM pg_catalog.pg_roles "
                "WHERE rolname = current_user",
                (self._config.graph_name,),
            )
            row = await cur.fetchone()
            if row is None or any(bool(value) for value in row):
                raise PermissionError(
                    "PostgreSQL runtime role must exist and must not have DDL privileges"
                )
            await cur.execute("SET search_path = public, ag_catalog, '$user';")
        await conn.commit()

    async def connect(self) -> None:
        """Opens a runtime-only connection pool without executing DDL."""
        if self._pool is not None:
            return

        async with self._connect_lock:
            if self._pool is not None:
                return

            pool: AsyncConnectionPool[AsyncConnection[Any]] = (
                AsyncConnectionPool(
                    conninfo=str(self._config.dsn),
                    min_size=self._config.min_connections,
                    max_size=self._config.max_connections,
                    open=False,
                    configure=self._configure_pooled_connection,
                )
            )
            await pool.open()
            self._pool = pool

            logger.info(
                "postgres_pool_initialized",
                min_size=self._config.min_connections,
                max_size=self._config.max_connections,
            )

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[Any]]:
        """Yields an active connection from the pool.

        Raises:
            RuntimeError: If the connection pool has not been initialized.
        """
        if self._pool is None:
            raise RuntimeError("Pool not initialized. Call connect() first.")

        async with self._pool.connection() as conn:
            yield conn

    async def close(self) -> None:
        """Closes the connection pool."""
        async with self._connect_lock:
            if self._pool:
                await self._pool.close()
                self._pool = None
                logger.info("postgres_pool_closed")

    async def __aenter__(self) -> PostgresClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
