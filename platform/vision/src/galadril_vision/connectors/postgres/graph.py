"""Apache AGE and TimescaleDB data access interface."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, LiteralString

import orjson
import structlog
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
    from galadril_vision.common.config import PostgresConnectorConfig
    from galadril_vision.connectors.postgres.client import PostgresClient

logger = structlog.get_logger(__name__)

_SYSTEM_TENANT_ID = "galadril-system"
_CYPHER_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _cypher_identifier(value: str) -> LiteralString:
    """Validates alphanumeric formatting of a Cypher label or relationship identifier."""
    if not _CYPHER_IDENTIFIER_RE.fullmatch(value):
        raise GraphOperationError(
            "cypher_identifier",
            f"invalid Cypher identifier: {value!r}",
        )
    return value


def _cypher_set_clause(
    alias: str, properties: dict[str, Any]
) -> tuple[sql.Composable, dict[str, Any]]:
    """Builds a parameterized SET clause from raw dictionary properties."""
    assignments: list[sql.Composable] = []
    params: dict[str, Any] = {}
    alias_sql = sql.SQL(_cypher_identifier(alias))

    for index, key in enumerate(sorted(properties)):
        param_name = f"p_{index}"
        assignments.append(
            sql.SQL("{}.{} = ${}").format(
                alias_sql,
                sql.SQL(_cypher_identifier(key)),
                sql.SQL(param_name),
            )
        )
        params[param_name] = properties[key]

    if not assignments:
        return sql.SQL(""), params

    return sql.SQL("SET ") + sql.SQL(", ").join(assignments), params


class GraphStore:
    """Handles data mutations across graph entities and relational time-series hyper-tables."""

    def __init__(
        self, client: PostgresClient, config: PostgresConnectorConfig
    ) -> None:
        """Initializes the store.

        Args:
            client: The Postgres connection client.
            config: Connector settings configuration.
        """
        self._client = client
        self._config = config
        self._graph_name = config.graph_name

    async def initialize(self) -> None:
        """Verifies that the target backend graph namespace exists."""
        async with self._client.maintenance_connection() as conn:
            query = sql.SQL("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = {graph_str}) THEN
                        PERFORM ag_catalog.create_graph({graph_str});
                    END IF;
                END $$;
            """).format(graph_str=sql.Literal(self._graph_name))
            await conn.execute(query)

        logger.info("eskg_store_initialized", graph=self._graph_name)

    def _vertex_params(self, vertex: GraphVertex) -> dict[str, Any]:
        """Normalizes tenant constraints and properties for a graph vertex."""
        tenant_id = normalize_tenant_id(vertex.tenant_id)
        props = vertex.properties.copy()
        if "tenant_id" in props:
            require_same_tenant(tenant_id, props["tenant_id"])
        props["tenant_id"] = tenant_id
        props["id"] = vertex.vertex_id
        return props

    def _edge_params(self, edge: GraphEdge) -> dict[str, Any]:
        """Normalizes tenant constraints and properties for a graph edge."""
        tenant_id = normalize_tenant_id(edge.tenant_id)
        props = edge.properties.copy()
        if "tenant_id" in props:
            require_same_tenant(tenant_id, props["tenant_id"])
        props["tenant_id"] = tenant_id
        props["source_id"] = edge.source_vertex_id
        props["target_id"] = edge.target_vertex_id
        return props

    async def ensure_vertex_on_connection(
        self, conn: AsyncConnection[Any], vertex: GraphVertex
    ) -> None:
        """Inserts or updates a vertex using an open connection transaction block."""
        props = self._vertex_params(vertex)
        set_clause, set_params = _cypher_set_clause("v", props)
        params = {
            "tenant_id": props["tenant_id"],
            "id": props["id"],
            **set_params,
        }
        query = sql.SQL("""
        SELECT * FROM cypher({graph}, $$
            MERGE (v:{label} {{tenant_id: $tenant_id, id: $id}})
            {set_clause}
            RETURN v
        $$, %s::agtype) AS (v agtype)
        """).format(
            graph=sql.Literal(self._graph_name),
            label=sql.SQL(_cypher_identifier(vertex.label)),
            set_clause=set_clause,
        )
        await conn.execute(query, (orjson.dumps(params).decode(),))

    async def ensure_vertex(self, vertex: GraphVertex) -> None:
        """Inserts or updates a vertex within a new transaction.

        Raises:
            GraphOperationError: If the execution fails.
        """
        props = vertex.properties.copy()
        if "tenant_id" not in props and vertex.tenant_id:
            props["tenant_id"] = vertex.tenant_id

        try:
            async with self._client.tenant_connection(vertex.tenant_id) as conn:
                async with conn.transaction():
                    await self.ensure_vertex_on_connection(conn, vertex)
        except Exception as exc:
            raise GraphOperationError("ensure_vertex", str(exc)) from exc

    async def create_edge_on_connection(
        self, conn: AsyncConnection[Any], edge: GraphEdge
    ) -> None:
        """Creates or updates a graph edge using an open connection transaction block."""
        props = self._edge_params(edge)
        edge_props = {
            key: value
            for key, value in props.items()
            if key not in {"source_id", "target_id"}
        }
        set_clause, set_params = _cypher_set_clause("r", edge_props)
        params = {
            "tenant_id": props["tenant_id"],
            "source_id": props["source_id"],
            "target_id": props["target_id"],
            **set_params,
        }
        query = sql.SQL("""
        SELECT * FROM cypher({graph}, $$
            MATCH (a {{tenant_id: $tenant_id, id: $source_id}})
            MATCH (b {{tenant_id: $tenant_id, id: $target_id}})
            MERGE (a)-[r:{edge_type}]->(b)
            {set_clause}
            RETURN r
        $$, %s::agtype) AS (r agtype)
        """).format(
            graph=sql.Literal(self._graph_name),
            edge_type=sql.SQL(_cypher_identifier(edge.edge_type)),
            set_clause=set_clause,
        )
        await conn.execute(query, (orjson.dumps(params).decode(),))

    async def create_edge(self, edge: GraphEdge) -> None:
        """Creates or updates a graph edge within a new transaction.

        Raises:
            GraphOperationError: If the execution fails.
        """
        try:
            async with self._client.tenant_connection(edge.tenant_id) as conn:
                async with conn.transaction():
                    await self.create_edge_on_connection(conn, edge)
        except Exception as exc:
            raise GraphOperationError("create_edge", str(exc)) from exc

    async def ensure_metric(self, metric_id: str, tenant_id: str) -> None:
        """Upserts a core system metric tracking vertex."""
        await self.ensure_vertex(
            GraphVertex(
                vertex_id=metric_id,
                label="Metric",
                tenant_id=tenant_id,
                properties={"name": metric_id},
            )
        )

    async def upsert_metric_influence(
        self,
        source_metric: str,
        target_metric: str,
        properties: dict[str, Any],
        tenant_id: str,
    ) -> None:
        """Upserts tracking vertices and connects them with an influence edge relationship."""
        await self.ensure_metric(source_metric, tenant_id)
        await self.ensure_metric(target_metric, tenant_id)
        await self.create_edge(
            GraphEdge(
                source_vertex_id=source_metric,
                target_vertex_id=target_metric,
                edge_type="INFLUENCE",
                tenant_id=tenant_id,
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
        tenant_id: str,
    ) -> list[str]:
        """Queries for neighbor vertex IDs within specified step distance constraints.

        Args:
            entity_id: Source node identifier.
            k_min: Minimum hop depth limit.
            k_max: Maximum hop depth limit.
            max_vertices: Caps the number of records returned.
            relationship_types: Permitted relationship names to traverse.
            tenant_id: Tenant context filtering identifier.

        Returns:
            A list of unique neighbor vertex identifiers.

        Raises:
            GraphOperationError: If the traversal query fails.
        """
        if not relationship_types:
            return []

        rel_union_sql = sql.SQL("|").join(
            sql.SQL(_cypher_identifier(r)) for r in relationship_types
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
            async with self._client.tenant_connection(tenant_id_val) as conn:
                query = sql.SQL("""
                      SELECT * FROM cypher({graph}, $$
                          MATCH (e {{tenant_id: $tenant_id, id: $entity_id}})
                          MATCH p=(e)-[:{rel_union}*1..$k_max]-(n)
                          WHERE n.tenant_id = $tenant_id
                          RETURN DISTINCT n.id, length(p)
                          LIMIT $max_vertices
                      $$, %s::agtype) AS (id agtype, hops agtype)
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
        k_min_val, k_max_val = int(k_min), int(k_max)
        for r in rows:
            if not r or len(r) < 2:
                continue
            try:
                hops = int(r[1])
                if k_min_val <= hops <= k_max_val:
                    node_id = str(r[0])
                    if node_id and node_id != entity_id:
                        out.append(node_id)
            except (ValueError, TypeError):
                continue
        return out

    async def get_event_ids_for_entities(
        self,
        entity_ids: list[str],
        window_start: datetime,
        window_end: datetime,
        max_events: int,
        relationship_types: tuple[str, ...],
        tenant_id: str,
    ) -> list[str]:
        """Retrieves linked event IDs matching specified time-window parameters.

        Args:
            entity_ids: Base target entities to search from.
            window_start: Lower bound timestamp filter.
            window_end: Upper bound timestamp filter.
            max_events: Maximum number of events to select.
            relationship_types: Edge filters linking entities to events.
            tenant_id: Tenant context filtering identifier.

        Returns:
            A list of matching event IDs.

        Raises:
            GraphOperationError: If the query execution fails.
        """
        if not entity_ids:
            return []

        rel_union_sql = sql.SQL("|").join(
            sql.SQL(_cypher_identifier(r)) for r in relationship_types
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
            async with self._client.tenant_connection(tenant_id_val) as conn:
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
                    $$, %s::agtype) AS (id agtype)
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

        return [str(r[0]) for r in rows if r and r[0] is not None]

    async def insert_event_on_connection(
        self, conn: AsyncConnection[Any], event: EventRecord
    ) -> None:
        """Inserts an event vertex and log table record using an active connection transaction."""
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
        """Inserts an event vertex and log table record inside a new transaction.

        Raises:
            GraphOperationError: If the insertions fail.
        """
        try:
            async with self._client.tenant_connection(event.tenant_id) as conn:
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
        role: str = "DERIVED_FROM",
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Creates a directional edge connecting an entity vertex to an event vertex."""
        await self.create_edge(
            GraphEdge(
                source_vertex_id=entity_id,
                target_vertex_id=event_id,
                edge_type=role,
                tenant_id=tenant_id,
                properties=properties or {},
            )
        )

    async def upsert_entity_observation_on_connection(
        self,
        conn: AsyncConnection[Any],
        *,
        vertex: GraphVertex,
        event: EventRecord,
        edge_type: str,
        state_type: str,
        state_value: dict[str, Any],
    ) -> None:
        """Saves a composite entity state alteration tuple using an active transaction connection."""
        tenant_id = require_same_tenant(vertex.tenant_id, event.tenant_id)
        await self.insert_event_on_connection(conn, event)
        await self.ensure_vertex_on_connection(conn, vertex)
        await self.create_edge_on_connection(
            conn,
            GraphEdge(
                source_vertex_id=vertex.vertex_id,
                target_vertex_id=event.event_id,
                edge_type=edge_type,
                tenant_id=tenant_id,
                properties={"state_type": state_type},
            ),
        )
        await self.insert_entity_state_on_connection(
            conn,
            EntityStateRecord(
                entity_id=vertex.vertex_id,
                event_id=event.event_id,
                state_type=state_type,
                state_value=state_value,
                event_time=event.timestamp,
                tenant_id=tenant_id,
            ),
        )

    async def insert_entity_state_on_connection(
        self, conn: AsyncConnection[Any], state: EntityStateRecord
    ) -> None:
        """Appends a structured state record into a hyper-table using an active transaction connection."""
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
        """Appends a single structured state snapshot into a database hyper-table."""
        async with self._client.tenant_connection(state.tenant_id) as conn:
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
        conn: AsyncConnection[Any],
        states: list[EntityStateRecord],
        *,
        expected_tenant_id: str,
    ) -> None:
        """Executes a batch multi-row insertion into a database state hyper-table connection."""
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
        """Executes a transactional batch insert for state hyper-table metric entities."""
        if not states:
            return

        tenant_id = normalize_tenant_id(states[0].tenant_id)
        async with self._client.tenant_connection(tenant_id) as conn:
            async with conn.transaction():
                await self.insert_entity_states_batch_on_connection(
                    conn, states, expected_tenant_id=tenant_id
                )

        logger.debug(
            "entity_states_batch_inserted",
            tenant_id=tenant_id,
            count=len(states),
        )
