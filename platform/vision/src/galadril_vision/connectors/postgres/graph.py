"""Postgres graph (AGE) handler."""

from __future__ import annotations
from typing import TYPE_CHECKING, Any

import orjson
import structlog
from datetime import datetime
from psycopg import sql

from galadril_vision.common.exceptions import GraphOperationError
from galadril_vision.common.types import (
    EntityStateRecord,
    EventRecord,
    GraphEdge,
    GraphVertex,
)

if TYPE_CHECKING:
    from galadril_vision.common.config import PostgresConfig
    from galadril_vision.connectors.postgres.client import PostgresClient

logger = structlog.get_logger(__name__)

_STATES_TABLE_SQL = """
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS postgis CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_trgm CASCADE;

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

SELECT create_hypertable(
    'entity_states',
    'event_time',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

ALTER TABLE entity_states SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tenant_id, entity_id, state_type',
    timescaledb.compress_orderby = 'event_time DESC'
);

SELECT add_compression_policy('entity_states', INTERVAL '30 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_entity_states_tenant_entity_time
ON entity_states (tenant_id, entity_id, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_entity_states_geom
ON entity_states USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_entity_states_name_trgm
ON entity_states
USING GIN ((state_value->>'name') gin_trgm_ops);
"""

_EVENTS_TABLE_SQL = """
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

CREATE TABLE IF NOT EXISTS eskg_events (
    event_id    TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    event_time  TIMESTAMPTZ NOT NULL,
    properties  JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (event_id, event_time)
);

SELECT create_hypertable(
    'eskg_events',
    'event_time',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

ALTER TABLE eskg_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'tenant_id, event_type',
    timescaledb.compress_orderby = 'event_time DESC'
);

SELECT add_compression_policy('eskg_events', INTERVAL '30 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_eskg_events_tenant_type_time
ON eskg_events (tenant_id, event_type, event_time DESC);
"""


class GraphStore:
    def __init__(self, client: PostgresClient, config: PostgresConfig) -> None:
        self._client = client
        self._config = config
        self._graph_name = config.graph_name

    async def initialize(self) -> None:
        async with self._client.connection() as conn:
            # This is the third time we execute this. I hope it works... (JK).
            await conn.execute("LOAD 'age'")
            await conn.execute("SET search_path = ag_catalog, public")
            query = sql.SQL("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = {graph_str}) THEN
                        PERFORM ag_catalog.create_graph({graph_str});
                    END IF;
                END $$;
            """).format(graph_str=sql.Literal(self._graph_name))
            await conn.execute(query)
            await conn.execute(_STATES_TABLE_SQL)
            await conn.execute(_EVENTS_TABLE_SQL)

        logger.info("eskg_store_initialized", graph=self._graph_name)

    async def ensure_vertex(self, vertex: GraphVertex) -> None:
        props = vertex.properties.copy()
        props["id"] = vertex.vertex_id
        params = orjson.dumps({"props": props}).decode()

        try:
            async with self._client.connection() as conn:
                query = sql.SQL("""
                SELECT * FROM cypher({graph}, $$
                    MERGE (v:{label} {{id: $props.id}})
                    SET v += $props
                    RETURN v
                $$, %s) AS (v agtype)
                """).format(
                    graph=sql.Literal(self._graph_name),
                    label=sql.Identifier(vertex.label),
                )
                await conn.execute(query, (params,))
        except Exception as exc:
            raise GraphOperationError("ensure_vertex", str(exc)) from exc

    async def create_edge(self, edge: GraphEdge) -> None:
        params = orjson.dumps(
            {
                "source_id": edge.source_vertex_id,
                "target_id": edge.target_vertex_id,
                "props": edge.properties,
            }
        ).decode()

        try:
            async with self._client.connection() as conn:
                query = sql.SQL("""
                SELECT * FROM cypher({graph}, $$
                    MATCH (a {{id: $source_id}})
                    MATCH (b {{id: $target_id}})
                    MERGE (a)-[r:{edge_type}]->(b)
                    SET r += $props
                    RETURN r
                $$, %s) AS (r agtype)
                """).format(
                    graph=sql.Literal(self._graph_name),
                    edge_type=sql.Identifier(edge.edge_type),
                )
                await conn.execute(query, (params,))
        except Exception as exc:
            raise GraphOperationError("create_edge", str(exc)) from exc

    async def ensure_metric(self, metric_id: str) -> None:
        await self.ensure_vertex(
            GraphVertex(
                vertex_id=metric_id,
                label="Metric",
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
    ) -> list[str]:
        rels = relationship_types[:]
        if not rels:
            return []

        rel_union_sql = sql.SQL("|").join(
            sql.Identifier(r) for r in relationship_types
        )

        params = orjson.dumps(
            {
                "entity_id": entity_id,
                "k_max": int(k_max),
                "max_vertices": int(max_vertices),
            }
        ).decode()

        try:
            async with self._client.connection() as conn:
                query = sql.SQL("""
                    SELECT * FROM cypher({graph}, $$
                        MATCH (e {{id: $entity_id}})
                        MATCH p=(e)-[:{rel_union}*1..$k_max]-(n)
                        RETURN DISTINCT n.id, length(p)
                        LIMIT $max_vertices
                    $$, %s) AS (id agtype, hops agtype)
                """).format(
                    graph=sql.Literal(self._graph_name),
                    rel_union=rel_union_sql,
                )
                result = await conn.execute(query, (params,))
                rows = await result.fetchall()
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
    ) -> list[str]:
        if not entity_ids:
            return []

        rel_union_sql = sql.SQL("|").join(
            sql.Literal(r) for r in relationship_types
        )
        params = orjson.dumps(
            {
                "entity_ids": entity_ids,
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
                        MATCH (ent {{id: eid}})-[:{rel_union}]->(ev)
                        WHERE exists(ev.timestamp)
                          AND ev.timestamp >= $window_start
                          AND ev.timestamp <= $window_end
                        RETURN DISTINCT ev.id
                        LIMIT $max_events
                    $$, %s) AS (id agtype)
                """).format(
                    graph_name=sql.Literal(self._graph_name),
                    rel_union=rel_union_sql,
                )
                result = await conn.execute(query, (params,))
                rows = await result.fetchall()
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

    async def insert_event(self, event: EventRecord) -> None:
        """Insert an Event (E) node into the Apache AGE graph."""
        props = event.properties.copy()
        props["timestamp"] = event.timestamp.isoformat()
        if event.location_coords:
            props["location"] = event.location_coords

        await self.ensure_vertex(
            GraphVertex(
                vertex_id=event.event_id,
                label=event.event_type.value,
                properties=props,
            )
        )

        try:
            async with self._client.connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO eskg_events (event_id, event_type, event_time, properties)
                    VALUES ($1, $2, $3, $4::jsonb)
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (
                        event.event_id,
                        event.event_type.value,
                        event.timestamp,
                        orjson.dumps(event.properties).decode(),
                    ),
                )
        except Exception as exc:
            raise GraphOperationError("insert_event", str(exc)) from exc

        logger.debug(
            "event_inserted", event_id=event.event_id, type=event.event_type
        )

    async def link_entity_to_event(
        self,
        entity_id: str,
        event_id: str,
        role: str = "PARTICIPATED_IN",
        properties: dict | None = None,
    ) -> None:
        """Link an Entity to an Event (e.g. PARTICIPATED_IN, MENTIONED_IN)."""
        await self.create_edge(
            GraphEdge(
                source_vertex_id=entity_id,
                target_vertex_id=event_id,
                edge_type=role,
                properties=properties or {},
            )
        )

    async def insert_entity_state(self, state: EntityStateRecord) -> None:
        """Store a State (S) triggered by an Event in the TimescaleDB hypertable with PostGIS support."""
        state_json = orjson.dumps(state.state_value).decode()

        # Extract location if present in the state to feed PostGIS.
        geom_wkt = None
        if "lat" in state.state_value and "lon" in state.state_value:
            # SRID=4326 is WGS 84 GPS standard. But we should be aware for
            # precise location: over the time, it could deviate.
            geom_wkt = f"SRID=4326;POINT({state.state_value['lon']} {state.state_value['lat']})"

        async with self._client.connection() as conn:
            query = sql.SQL("""
                INSERT INTO entity_states (entity_id, event_id, state_type, state_value, geom, event_time)
                VALUES ($1, $2, $3, $4::jsonb, ST_GeomFromEWKT($5), $6)
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
                ),
            )
        logger.debug(
            "entity_state_inserted",
            entity_id=state.entity_id,
            state_type=state.state_type,
        )

    async def insert_entity_states_batch(
        self, states: list[EntityStateRecord]
    ) -> None:
        """Store multiple States (S) in the TimescaleDB hypertable in a single batch."""
        if not states:
            return

        params = []
        for state in states:
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
                )
            )

        async with self._client.connection() as conn:
            query = sql.SQL("""
                INSERT INTO entity_states (entity_id, event_id, state_type, state_value, geom, event_time)
                VALUES ($1, $2, $3, $4::jsonb, ST_GeomFromEWKT($5), $6)
            """)
            async with conn.cursor() as cur:
                await cur.executemany(query, params)

        logger.debug("entity_states_batch_inserted", count=len(states))
