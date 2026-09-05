"""PostgreSQL async connection pool client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from galadril_vision.common.types import normalize_tenant_id
from galadril_vision.connectors.postgres.models import EMBEDDING_DIM, Base
from galadril_vision.connectors.postgres.schema import vision_schema_sql

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
        self._pool: AsyncConnectionPool[AsyncConnection[TupleRow]] | None = None
        self._maintenance_pool: (
            AsyncConnectionPool[AsyncConnection[TupleRow]] | None
        ) = None
        self._connect_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()

    @staticmethod
    async def _configure_pooled_connection(
        conn: AsyncConnection[TupleRow],
    ) -> None:
        """Initializes runtime session configurations on a new connection."""
        async with conn.cursor() as cur:
            await cur.execute("LOAD 'age';")
            await cur.execute("SET search_path = public, ag_catalog, '$user';")
        await conn.commit()

    async def connect(
        self, *, initialize_database_infrastructure: bool = False
    ) -> None:
        """Opens the connection pool and optionally provisions extensions and schemas.

        Args:
            initialize_database_infrastructure: True to provision the current
                schema and extensions. Defaults to False.

        Raises:
            Exception: If pool initialization or database provisioning fails.
        """
        if self._pool is not None:
            return

        async with self._connect_lock:
            if self._pool is not None:
                return

            pool: AsyncConnectionPool[AsyncConnection[TupleRow]] = (
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
        """Creates operational tables and applies Vision-owned SQL resources.

        Raises:
            ValueError: If runtime vector dimensions differ from the migrated
                schema.
        """
        sa_dsn = str(self._config.dsn).replace(
            "postgresql://", "postgresql+psycopg://"
        )

        if int(self._config.vector_dimensions) != EMBEDDING_DIM:
            raise ValueError(
                "Vision vector dimensions must match the migrated "
                f"VECTOR({EMBEDDING_DIM}) schema"
            )

        engine = create_async_engine(sa_dsn)

        async with engine.begin() as sa_conn:
            schema_statements = iter(vision_schema_sql())
            extension_statement = next(schema_statements, None)
            if extension_statement is None:
                raise RuntimeError(
                    "Vision PostgreSQL resources are unavailable"
                )
            await sa_conn.execute(text(extension_statement))

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
            for statement in schema_statements:
                await sa_conn.execute(text(statement))

        await engine.dispose()
        logger.info("postgres_operational_schema_initialized", graph=graph_name)

    @asynccontextmanager
    async def tenant_connection(
        self, tenant_id: str
    ) -> AsyncIterator[AsyncConnection[TupleRow]]:
        """Yields a transaction after installing fail-closed RLS context.

        Raises:
            RuntimeError: If the connection pool has not been initialized.
        """
        if self._pool is None:
            raise RuntimeError("Pool not initialized. Call connect() first.")

        tenant_id_val = normalize_tenant_id(tenant_id)
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    (tenant_id_val,),
                )
                yield conn

    @asynccontextmanager
    async def maintenance_connection(
        self,
    ) -> AsyncIterator[AsyncConnection[TupleRow]]:
        """Yields an unscoped connection using a constrained non-superuser role.

        The optional maintenance identity may bypass RLS only for explicitly
        granted background-work tables. Falling back to the application identity
        preserves fail-closed behavior when no maintenance identity is configured.
        """
        if self._pool is None:
            raise RuntimeError("Pool not initialized. Call connect() first.")
        maintenance_dsn = self._config.maintenance_dsn
        connection_dsn = (
            maintenance_dsn
            if isinstance(maintenance_dsn, str) and maintenance_dsn
            else str(self._config.dsn)
        )
        async with self._maintenance_lock:
            if self._maintenance_pool is None:
                pool: AsyncConnectionPool[AsyncConnection[TupleRow]] = (
                    AsyncConnectionPool(
                        conninfo=connection_dsn,
                        min_size=1,
                        max_size=2,
                        open=False,
                        configure=self._configure_pooled_connection,
                    )
                )
                await pool.open()
                self._maintenance_pool = pool
        if self._maintenance_pool is None:
            raise RuntimeError("Maintenance pool initialization failed")
        async with self._maintenance_pool.connection() as conn:
            yield conn

    async def close(self) -> None:
        """Closes the connection pool."""
        async with self._connect_lock:
            if self._pool:
                await self._pool.close()
                self._pool = None
            if self._maintenance_pool:
                await self._maintenance_pool.close()
                self._maintenance_pool = None
            logger.info("postgres_pool_closed")

    async def __aenter__(self) -> PostgresClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
