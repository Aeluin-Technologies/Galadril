from __future__ import annotations
from typing import TYPE_CHECKING

import orjson
import structlog
from psycopg import sql

from common.exceptions import GraphOperationError
from common.types import (
    EntityStateRecord,
    EventRecord,
    GraphEdge,
    GraphVertex,
)

if TYPE_CHECKING:
    from common.config import PostgresConfig
    from connectors.postgres.client import PostgresClient

logger = structlog.get_logger(__name__)

_STATES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS entity_states (
    entity_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    state_type TEXT NOT NULL,
    state_value JSONB NOT NULL,
    geom GEOMETRY(Point, 4326),
    event_time TIMESTAMPTZ NOT NULL,
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
    timescaledb.compress_segmentby = 'entity_id, state_type',
    timescaledb.compress_orderby = 'event_time DESC'
);

SELECT add_compression_policy('entity_states', INTERVAL '30 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_entity_states_entity ON entity_states (entity_id, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_entity_states_geom ON entity_states USING GIST (geom);
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
