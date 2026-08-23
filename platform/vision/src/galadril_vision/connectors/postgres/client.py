"""PostgreSQL async connection pool client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from galadril_vision.common.types import normalize_tenant_id
from galadril_vision.connectors.postgres.models import Base

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
        self._maintenance_pool: (
            AsyncConnectionPool[AsyncConnection[Any]] | None
        ) = None
        self._connect_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()

    @staticmethod
    async def _configure_pooled_connection(conn: AsyncConnection[Any]) -> None:
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
        """Creates required relational schemas, graph metadata, and extensions."""
        maintenance_dsn = self._config.maintenance_dsn
        if not isinstance(maintenance_dsn, str) or not maintenance_dsn:
            raise RuntimeError(
                "Database initialization requires separate maintenance credentials"
            )
        sa_dsn = maintenance_dsn.replace(
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
        """Executes secondary DDL statements not captured by standard metadata tables."""
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
            """
            DO $rls$
            DECLARE
                protected_table TEXT;
            BEGIN
                FOREACH protected_table IN ARRAY ARRAY[
                    'entity_embeddings', 'eskg_events', 'entity_states',
                    'causal_runs', 'pipeline_executions', 'authz_outbox',
                    'identity_links'
                ] LOOP
                    EXECUTE format(
                        'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',
                        protected_table
                    );
                    EXECUTE format(
                        'ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',
                        protected_table
                    );
                    EXECUTE format(
                        'DROP POLICY IF EXISTS tenant_isolation ON public.%I',
                        protected_table
                    );
                    EXECUTE format(
                        'CREATE POLICY tenant_isolation ON public.%I FOR ALL '
                        'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
                        'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))',
                        protected_table
                    );
                    EXECUTE format(
                        'REVOKE ALL ON public.%I FROM PUBLIC', protected_table
                    );
                END LOOP;
            END
            $rls$
            """,
        )
        for statement in statements:
            await conn.execute(text(statement))

    @asynccontextmanager
    async def tenant_connection(
        self, tenant_id: str
    ) -> AsyncIterator[AsyncConnection[Any]]:
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
    ) -> AsyncIterator[AsyncConnection[Any]]:
        """Yields the explicit privileged path for schema/outbox maintenance.

        The configured maintenance identity must be separate from normal
        application roles and is expected to be tightly scoped by deployment.
        """
        if self._pool is None:
            raise RuntimeError("Pool not initialized. Call connect() first.")
        maintenance_dsn = self._config.maintenance_dsn
        if not isinstance(maintenance_dsn, str) or not maintenance_dsn:
            raise RuntimeError(
                "Maintenance connection requires separate maintenance credentials"
            )
        async with self._maintenance_lock:
            if self._maintenance_pool is None:
                pool: AsyncConnectionPool[AsyncConnection[Any]] = (
                    AsyncConnectionPool(
                        conninfo=maintenance_dsn,
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
