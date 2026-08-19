"""Explicit privileged PostgreSQL infrastructure provisioning."""

from __future__ import annotations

from typing import Any, cast

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from galadril_vision.common.config import (
    PostgresConnectorConfig,
    PostgresProvisioningConfig,
)
from galadril_vision.connectors.postgres.models import Base

logger = structlog.get_logger(__name__)

_EXTENSIONS = (
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


class PostgresProvisioner:
    """Provisions infrastructure using credentials isolated from runtime clients."""

    def __init__(
        self,
        admin_config: PostgresProvisioningConfig,
        runtime_config: PostgresConnectorConfig,
    ) -> None:
        """Initializes an explicit admin-to-runtime provisioning boundary."""
        if admin_config.user == runtime_config.user:
            raise ValueError(
                "provisioning and runtime PostgreSQL roles must be different"
            )
        self._admin_config = admin_config
        self._runtime_config = runtime_config

    async def provision(self) -> None:
        """Creates database objects and grants only operational privileges."""
        self._configure_vector_dimensions()
        engine = create_async_engine(self._admin_config.sqlalchemy_dsn)
        try:
            async with engine.begin() as conn:
                await self._validate_runtime_role(conn)
                await self._create_extensions(conn)
                await conn.execute(text("LOAD 'age'"))
                await conn.execute(
                    text("SET LOCAL search_path = public, ag_catalog, '$user'")
                )
                await self._create_graph(conn)
                await conn.run_sync(Base.metadata.create_all)
                await self._ensure_schema_invariants(conn)
                await self._grant_runtime_privileges(conn)
                await self._validate_effective_runtime_privileges(conn)
        finally:
            await engine.dispose()

        logger.info(
            "postgres_infrastructure_provisioned",
            graph=self._runtime_config.graph_name,
            runtime_role=self._runtime_config.user,
        )

    def _configure_vector_dimensions(self) -> None:
        """Applies the deployment dimension before SQLAlchemy emits table DDL."""
        for column in Base.metadata.tables["entity_embeddings"].columns:
            if column.name == "embedding" and hasattr(column.type, "dimensions"):
                cast(Any, column.type).dimensions = int(
                    self._runtime_config.vector_dimensions
                )

    async def _validate_runtime_role(self, conn: AsyncConnection) -> None:
        """Rejects missing or superuser runtime roles before provisioning."""
        result = await conn.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole "
                "FROM pg_catalog.pg_roles WHERE rolname = :role"
            ),
            {"role": self._runtime_config.user},
        )
        row = result.fetchone()
        if row is None:
            raise ValueError("runtime PostgreSQL role does not exist")
        if any(bool(value) for value in row):
            raise ValueError(
                "runtime PostgreSQL role must not have administrative attributes"
            )

    async def _create_extensions(self, conn: AsyncConnection) -> None:
        """Installs the fixed allowlist of server extensions."""
        for extension in _EXTENSIONS:
            await conn.execute(
                text(f"CREATE EXTENSION IF NOT EXISTS {extension} CASCADE")
            )

    async def _create_graph(self, conn: AsyncConnection) -> None:
        """Creates the configured AGE graph when absent."""
        result = await conn.execute(
            text("SELECT 1 FROM ag_catalog.ag_graph WHERE name = :name"),
            {"name": self._runtime_config.graph_name},
        )
        if result.fetchone() is None:
            await conn.execute(
                text("SELECT ag_catalog.create_graph(:name)"),
                {"name": self._runtime_config.graph_name},
            )

    async def _ensure_schema_invariants(self, conn: AsyncConnection) -> None:
        """Creates secondary indexes not represented by model metadata."""
        statements = (
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_authz_outbox_tenant_object "
            "ON authz_outbox (tenant_id, object_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_eskg_events_tenant_event_time "
            "ON eskg_events (tenant_id, event_id, event_time)",
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ux_entity_embeddings_tenant_id_id_created_at "
            "ON entity_embeddings (tenant_id, id, created_at)",
        )
        for statement in statements:
            await conn.execute(text(statement))

    async def _grant_runtime_privileges(self, conn: AsyncConnection) -> None:
        """Grants DML-only access to the runtime role and graph schema."""
        preparer = conn.dialect.identifier_preparer
        role = preparer.quote_identifier(self._runtime_config.user)
        graph = preparer.quote_identifier(self._runtime_config.graph_name)
        schemas = f"public, ag_catalog, {graph}"
        statements = (
            f"ALTER ROLE {role} SET session_preload_libraries = 'age'",
            f"REVOKE CREATE ON DATABASE "
            f"{preparer.quote_identifier(self._runtime_config.database)} "
            f"FROM {role}",
            f"REVOKE CREATE ON SCHEMA public, {graph} FROM {role}",
            f"GRANT USAGE ON SCHEMA {schemas} TO {role}",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO {role}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
            f"public, {graph} TO {role}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public, {graph} "
            f"TO {role}",
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public, {graph} GRANT "
            f"SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public, {graph} GRANT "
            f"USAGE, SELECT ON SEQUENCES TO {role}",
        )
        for statement in statements:
            await conn.execute(text(statement))

    async def _validate_effective_runtime_privileges(
        self, conn: AsyncConnection
    ) -> None:
        """Fails closed when inherited grants still permit runtime DDL."""
        result = await conn.execute(
            text(
                "SELECT "
                "has_database_privilege(:role, current_database(), 'CREATE'), "
                "has_schema_privilege(:role, 'public', 'CREATE'), "
                "has_schema_privilege(:role, :graph, 'CREATE')"
            ),
            {
                "role": self._runtime_config.user,
                "graph": self._runtime_config.graph_name,
            },
        )
        row = result.fetchone()
        if row is None or any(bool(value) for value in row):
            raise ValueError(
                "runtime PostgreSQL role retains DDL privileges through "
                "PUBLIC or inherited role grants"
            )
