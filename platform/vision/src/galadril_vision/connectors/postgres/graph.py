"""Postgres graph (AGE) handler."""

from __future__ import annotations
from typing import TYPE_CHECKING, Any

import orjson
import structlog
from datetime import datetime
from psycopg import AsyncConnection, sql

from galadril_vision.common.exceptions import GraphOperationError
from galadril_vision.common.types import (
    EntityStateRecord,
    EventRecord,
    GraphEdge,
    GraphVertex,
    normalize_tenant_id,
    require_same_tenant,
)

if TYPE_CHECKING:
    from galadril_vision.common.config import PostgresConfig
    from galadril_vision.connectors.postgres.client import PostgresClient

logger = structlog.get_logger(__name__)

_SYSTEM_TENANT_ID = "galadril-system"

_SQL_CREATE_STATES_TABLE = """
CREATE TABLE IF NOT EXISTS entity_states (
    tenant_id   TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    state_type  TEXT NOT NULL,
    state_value JSONB NOT NULL,
    geom        GEOMETRY(Point, 4326),
    event_time  TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);
"""

_SQL_CREATE_STATES_HYPERTABLE = """
SELECT create_hypertable(
    'entity_states',
    'event_time',
    if_not_exists => TRUE,
    migrate_data => TRUE
);
"""

_SQL_CONFIGURE_STATES_COMPRESSION = """
ALTER TABLE entity_states SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tenant_id, entity_id, state_type',
    timescaledb.compress_orderby = 'event_time DESC'
);
SELECT add_compression_policy('entity_states', INTERVAL '30 days', if_not_exists => TRUE);
"""

_SQL_CREATE_STATES_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_entity_states_tenant_entity_time
ON entity_states (tenant_id, entity_id, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_entity_states_geom
ON entity_states USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_entity_states_name_trgm
ON entity_states
USING GIN ((state_value->>'name') gin_trgm_ops);
"""

_SQL_CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS eskg_events (
    event_id    TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    event_time  TIMESTAMPTZ NOT NULL,
    properties  JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (tenant_id, event_id, event_time)
);
"""

_SQL_CREATE_EVENTS_HYPERTABLE = """
SELECT create_hypertable(
    'eskg_events',
    'event_time',
    if_not_exists => TRUE,
    migrate_data => TRUE
);
"""

_SQL_CONFIGURE_EVENTS_COMPRESSION = """
ALTER TABLE eskg_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tenant_id, event_type',
    timescaledb.compress_orderby = 'event_time DESC'
);
SELECT add_compression_policy('eskg_events', INTERVAL '30 days', if_not_exists => TRUE);
"""

_SQL_CREATE_EVENTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_eskg_events_tenant_type_time
ON eskg_events (tenant_id, event_type, event_time DESC);
"""

_SQL_MIGRATE_EVENTS_TENANT_PK = """
DO $$
DECLARE
    pk_cols TEXT;
BEGIN
    SELECT string_agg(a.attname, ',' ORDER BY cols.ordinality)
    INTO pk_cols
    FROM pg_constraint c
    JOIN unnest(c.conkey) WITH ORDINALITY AS cols(attnum, ordinality)
        ON TRUE
    JOIN pg_attribute a
        ON a.attrelid = c.conrelid AND a.attnum = cols.attnum
    WHERE c.conrelid = 'eskg_events'::regclass
      AND c.contype = 'p';

    IF pk_cols = 'event_id,event_time' THEN
        ALTER TABLE eskg_events DROP CONSTRAINT eskg_events_pkey;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'eskg_events'::regclass
          AND contype = 'p'
    ) THEN
        ALTER TABLE eskg_events
        ADD CONSTRAINT eskg_events_pkey
        PRIMARY KEY (tenant_id, event_id, event_time);
    END IF;
END $$;
"""


class GraphStore:
    """Tenant-aware Apache AGE and TimescaleDB graph store."""

    def __init__(self, client: PostgresClient, config: PostgresConfig) -> None:
        self._client = client
        self._config = config
        self._graph_name = config.graph_name

    async def initialize(self) -> None:
        async with self._client.connection() as conn:
            await self.prepare_connection(conn)

            await conn.execute(
                "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"
            )
            await conn.execute(
                "CREATE EXTENSION IF NOT EXISTS postgis CASCADE;"
            )
            await conn.execute(
                "CREATE EXTENSION IF NOT EXISTS pg_trgm CASCADE;"
            )

            query = sql.SQL("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = {graph_str}) THEN
                        PERFORM ag_catalog.create_graph({graph_str});
                    END IF;
                END $$;
            """).format(graph_str=sql.Literal(self._graph_name))
            await conn.execute(query)

            await conn.execute(_SQL_CREATE_STATES_TABLE)
            await conn.execute(_SQL_CREATE_STATES_HYPERTABLE)
            await conn.execute(_SQL_CONFIGURE_STATES_COMPRESSION)
            await conn.execute(_SQL_CREATE_STATES_INDEXES)

            await conn.execute(_SQL_CREATE_EVENTS_TABLE)
            await conn.execute(_SQL_MIGRATE_EVENTS_TENANT_PK)
            await conn.execute(_SQL_CREATE_EVENTS_HYPERTABLE)
            await conn.execute(_SQL_CONFIGURE_EVENTS_COMPRESSION)
            await conn.execute(_SQL_CREATE_EVENTS_INDEXES)

        logger.info("eskg_store_initialized", graph=self._graph_name)

    async def prepare_connection(self, conn: AsyncConnection) -> None:
        """Prepare connection-local AGE state for graph operations."""
        await conn.execute("LOAD 'age'")
        await conn.execute("SET search_path = ag_catalog, public, '$user'")

    def _vertex_params(self, vertex: GraphVertex) -> tuple[str, str]:
        tenant_id = normalize_tenant_id(vertex.tenant_id)
        props = vertex.properties.copy()
        if "tenant_id" in props:
            require_same_tenant(tenant_id, props["tenant_id"])
        props["tenant_id"] = tenant_id
        props["id"] = vertex.vertex_id
        return tenant_id, orjson.dumps({"props": props}).decode()

    def _edge_params(self, edge: GraphEdge) -> tuple[str, str]:
        tenant_id = normalize_tenant_id(edge.tenant_id)
        props = edge.properties.copy()
        if "tenant_id" in props:
            require_same_tenant(tenant_id, props["tenant_id"])
        props["tenant_id"] = tenant_id
        return tenant_id, orjson.dumps(
            {
                "tenant_id": tenant_id,
                "source_id": edge.source_vertex_id,
                "target_id": edge.target_vertex_id,
                "props": props,
            }
        ).decode()

    async def ensure_vertex_on_connection(
        self, conn: AsyncConnection, vertex: GraphVertex
    ) -> None:
        """Create or update a vertex in the caller's transaction."""
        _, params = self._vertex_params(vertex)
        query = sql.SQL("""
        SELECT * FROM cypher({graph}, $$
            MERGE (v:{label} {{tenant_id: $props.tenant_id, id: $props.id}})
            SET v += $props
            RETURN v
        $$, %s) AS (v agtype)
        """).format(
            graph=sql.Literal(self._graph_name),
            label=sql.Identifier(vertex.label),
        )
        await self.prepare_connection(conn)
        await conn.execute(query, (params,))

    async def ensure_vertex(self, vertex: GraphVertex) -> None:
        props = vertex.properties.copy()
        if "tenant_id" not in props and vertex.tenant_id:
            props["tenant_id"] = vertex.tenant_id

        try:
            async with self._client.connection() as conn:
                async with conn.transaction():
                    await self.ensure_vertex_on_connection(
                        conn,
                        GraphVertex(
                            vertex_id=vertex.vertex_id,
                            label=vertex.label,
                            tenant_id=vertex.tenant_id,
                            properties=props,
                        ),
                    )
        except Exception as exc:
            raise GraphOperationError("ensure_vertex", str(exc)) from exc

    async def create_edge_on_connection(
        self, conn: AsyncConnection, edge: GraphEdge
    ) -> None:
        """Create or update a tenant-scoped graph edge in a transaction."""
        _, params = self._edge_params(edge)
        query = sql.SQL("""
        SELECT * FROM cypher({graph}, $$
            MATCH (a {{tenant_id: $tenant_id, id: $source_id}})
            MATCH (b {{tenant_id: $tenant_id, id: $target_id}})
            MERGE (a)-[r:{edge_type}]->(b)
            SET r += $props
            RETURN r
        $$, %s) AS (r agtype)
        """).format(
            graph=sql.Literal(self._graph_name),
            edge_type=sql.Identifier(edge.edge_type),
        )
        await self.prepare_connection(conn)
        await conn.execute(query, (params,))

    async def create_edge(self, edge: GraphEdge) -> None:
        try:
            async with self._client.connection() as conn:
                async with conn.transaction():
                    await self.create_edge_on_connection(conn, edge)
        except Exception as exc:
            raise GraphOperationError("create_edge", str(exc)) from exc

    async def ensure_metric(self, metric_id: str) -> None:
        await self.ensure_vertex(
            GraphVertex(
                vertex_id=metric_id,
                label="Metric",
                tenant_id=_SYSTEM_TENANT_ID,
                properties={"name": metric_id},
            )
        )

    async def upsert_metric_influence(
        self,
        source_metric: str,
        target_metric: str,
        properties: dict[str, Any],
    ) -> None:
        await self.ensure_metric(source_metric)
        await self.ensure_metric(target_metric)
        await self.create_edge(
            GraphEdge(
                source_vertex_id=source_metric,
                target_vertex_id=target_metric,
                edge_type="INFLUENCE",
                tenant_id=_SYSTEM_TENANT_ID,
                properties=properties,
            )
        )

    async def get_entity_k_hop_neighbors(
        self,
        entity_id: str,
        k_min: int,
        k_max: int,
        max_vertices: int,
        relationship_types: list[str],
        tenant_id: str = _SYSTEM_TENANT_ID,
    ) -> list[str]:
        rels = relationship_types[:]
        if not rels:
            return []

        rel_union_sql = sql.SQL("|").join(
            sql.Identifier(r) for r in relationship_types
        )

        tenant_id_val = normalize_tenant_id(tenant_id)
        params = orjson.dumps(
            {
                "entity_id": entity_id,
                "tenant_id": tenant_id_val,
                "k_max": int(k_max),
                "max_vertices": int(max_vertices),
            }
        ).decode()

        try:
            async with self._client.connection() as conn:
                query = sql.SQL("""
                      SELECT * FROM cypher({graph}, $$
                          MATCH (e {{tenant_id: $tenant_id, id: $entity_id}})
                          MATCH p=(e)-[:{rel_union}*1..$k_max]-(n)
                          WHERE n.tenant_id = $tenant_id
                          RETURN DISTINCT n.id, length(p)
                          LIMIT $max_vertices
                      $$, %s) AS (id agtype, hops agtype)
                """).format(
                    graph=sql.Literal(self._graph_name),
                    rel_union=rel_union_sql,
                )
                async with conn.cursor() as cur:
                    await cur.execute(query, (params,))
                    rows = await cur.fetchall()
        except Exception as exc:
            raise GraphOperationError(
                "get_entity_k_hop_neighbor", str(exc)
            ) from exc

        out: list[str] = []
        for r in rows:
            if not r or len(r) < 2:
                continue
            node_id_raw, hops_raw = r[0], r[1]
            try:
                hops = int(hops_raw)
            except Exception:
                continue
            if hops < int(k_min) or hops > int(k_max):
                continue
            try:
                node_id = str(node_id_raw)
            except Exception:
                continue
            if node_id and node_id != entity_id:
                out.append(node_id)
        return out

    async def get_event_ids_for_entities(
        self,
        entity_ids: list[str],
        window_start: datetime,
        window_end: datetime,
        max_events: int,
        relationship_types: tuple[str, ...],
        tenant_id: str = _SYSTEM_TENANT_ID,
    ) -> list[str]:
        if not entity_ids:
            return []

        rel_union_sql = sql.SQL("|").join(
            sql.Identifier(r) for r in relationship_types
        )
        tenant_id_val = normalize_tenant_id(tenant_id)
        params = orjson.dumps(
            {
                "entity_ids": entity_ids,
                "tenant_id": tenant_id_val,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "max_events": int(max_events),
            }
        ).decode()

        try:
            async with self._client.connection() as conn:
                query = sql.SQL("""
                      SELECT * FROM cypher({graph_name}, $$
                          UNWIND $entity_ids AS eid
                          MATCH (ent {{tenant_id: $tenant_id, id: eid}})-[:{rel_union}]->(ev)
                          WHERE exists(ev.timestamp)
                            AND ev.tenant_id = $tenant_id
                            AND ev.timestamp >= $window_start
                            AND ev.timestamp <= $window_end
                        RETURN DISTINCT ev.id
                        LIMIT $max_events
                    $$, %s) AS (id agtype)
                """).format(
                    graph_name=sql.Literal(self._graph_name),
                    rel_union=rel_union_sql,
                )
                async with conn.cursor() as cur:
                    await cur.execute(query, (params,))
                    rows = await cur.fetchall()
        except Exception as exc:
            raise GraphOperationError(
                "get_event_ids_for_entities", str(exc)
            ) from exc

        out: list[str] = []
        for r in rows:
            if not r:
                continue
            try:
                out.append(str(r[0]))
            except Exception:
                continue
        return out

    async def insert_event_on_connection(
        self, conn: AsyncConnection, event: EventRecord
    ) -> None:
        """Insert an Event (E) node using the caller's transaction."""
        tenant_id = normalize_tenant_id(event.tenant_id)
        props = event.properties.copy()
        if "tenant_id" in props:
            require_same_tenant(tenant_id, props["tenant_id"])
        props["tenant_id"] = tenant_id
        props["timestamp"] = event.timestamp.isoformat()
        if event.location_coords:
            props["location"] = event.location_coords

        await self.ensure_vertex_on_connection(
            conn,
            GraphVertex(
                vertex_id=event.event_id,
                label=event.event_type.value,
                tenant_id=tenant_id,
                properties=props,
            ),
        )

        query = sql.SQL("""
            INSERT INTO eskg_events (
                tenant_id, event_id, event_type, event_time, properties
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, event_id, event_time) DO NOTHING
        """)
        await conn.execute(
            query,
            (
                tenant_id,
                event.event_id,
                event.event_type.value,
                event.timestamp,
                orjson.dumps(props).decode(),
            ),
        )

    async def insert_event(self, event: EventRecord) -> None:
        """Insert an Event (E) node into the Apache AGE graph."""
        try:
            async with self._client.connection() as conn:
                async with conn.transaction():
                    await self.insert_event_on_connection(conn, event)
        except Exception as exc:
            raise GraphOperationError("insert_event", str(exc)) from exc

        logger.debug(
            "event_inserted",
            tenant_id=event.tenant_id,
            event_id=event.event_id,
            type=event.event_type,
        )

    async def link_entity_to_event(
        self,
        entity_id: str,
        event_id: str,
        tenant_id: str,
        role: str = "PARTICIPATED_IN",
        properties: dict | None = None,
    ) -> None:
        """Link an Entity to an Event (e.g. PARTICIPATED_IN, MENTIONED_IN)."""
        await self.create_edge(
            GraphEdge(
                source_vertex_id=entity_id,
                target_vertex_id=event_id,
                edge_type=role,
                tenant_id=tenant_id,
                properties=properties or {},
            )
        )

    async def insert_entity_state_on_connection(
        self, conn: AsyncConnection, state: EntityStateRecord
    ) -> None:
        """Store a State (S) row using the caller's transaction."""
        tenant_id = normalize_tenant_id(state.tenant_id)
        state_json = orjson.dumps(state.state_value).decode()

        geom_wkt = None
        if "lat" in state.state_value and "lon" in state.state_value:
            geom_wkt = f"SRID=4326;POINT({state.state_value['lon']} {state.state_value['lat']})"

        query = sql.SQL("""
            INSERT INTO entity_states (
                entity_id, event_id, state_type, state_value, geom, event_time,
                tenant_id
            )
            VALUES (%s, %s, %s, %s::jsonb, ST_GeomFromEWKT(%s), %s, %s)
        """)
        await conn.execute(
            query,
            (
                state.entity_id,
                state.event_id,
                state.state_type,
                state_json,
                geom_wkt,
                state.event_time,
                tenant_id,
            ),
        )

    async def insert_entity_state(self, state: EntityStateRecord) -> None:
        """Store a State (S) row in the TimescaleDB hypertable."""
        async with self._client.connection() as conn:
            async with conn.transaction():
                await self.insert_entity_state_on_connection(conn, state)
        logger.debug(
            "entity_state_inserted",
            tenant_id=state.tenant_id,
            entity_id=state.entity_id,
            state_type=state.state_type,
        )

    async def insert_entity_states_batch_on_connection(
        self,
        conn: AsyncConnection,
        states: list[EntityStateRecord],
        *,
        expected_tenant_id: str,
    ) -> None:
        """Store multiple State (S) rows using the caller's transaction."""
        if not states:
            return

        tenant_id = normalize_tenant_id(expected_tenant_id)
        params = []
        for state in states:
            require_same_tenant(tenant_id, state.tenant_id)
            state_json = orjson.dumps(state.state_value).decode()
            geom_wkt = None
            if "lat" in state.state_value and "lon" in state.state_value:
                geom_wkt = f"SRID=4326;POINT({state.state_value['lon']} {state.state_value['lat']})"

            params.append(
                (
                    state.entity_id,
                    state.event_id,
                    state.state_type,
                    state_json,
                    geom_wkt,
                    state.event_time,
                    tenant_id,
                )
            )

        query = sql.SQL("""
            INSERT INTO entity_states (
                entity_id, event_id, state_type, state_value, geom, event_time,
                tenant_id
            )
            VALUES (%s, %s, %s, %s::jsonb, ST_GeomFromEWKT(%s), %s, %s)
        """)
        async with conn.cursor() as cur:
            await cur.executemany(query, params)

    async def insert_entity_states_batch(
        self, states: list[EntityStateRecord]
    ) -> None:
        """Store multiple State (S) rows in a single batch."""
        if not states:
            return

        tenant_id = normalize_tenant_id(states[0].tenant_id)
        for state in states:
            require_same_tenant(tenant_id, state.tenant_id)

        async with self._client.connection() as conn:
            async with conn.transaction():
                await self.insert_entity_states_batch_on_connection(
                    conn, states, expected_tenant_id=tenant_id
                )

        logger.debug(
            "entity_states_batch_inserted",
            tenant_id=tenant_id,
            count=len(states),
        )
