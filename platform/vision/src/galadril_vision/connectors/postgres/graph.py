"""Apache AGE and TimescaleDB data access interface."""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, LiteralString

import orjson
import structlog
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import JsonValue

from galadril_vision.common.eskg import (
    ONTOLOGY_KINDS,
    RELATION_ENDPOINTS,
    GraphNodeKind,
    relation_endpoints,
)
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
    alias: str, properties: dict[str, JsonValue]
) -> tuple[sql.Composable, dict[str, JsonValue]]:
    """Builds a parameterized SET clause from raw dictionary properties."""
    assignments: list[sql.Composable] = []
    params: dict[str, JsonValue] = {}
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


@lru_cache(maxsize=64)
def _endpoint_predicate(
    source: str,
    target: str,
    pairs: tuple[tuple[GraphNodeKind, GraphNodeKind], ...],
) -> sql.Composable:
    """Uses only contract-owned literals when constraining AGE endpoint roles."""
    return sql.SQL(" OR ").join(
        sql.SQL(
            "(label({source}) = {left} AND label({target}) = {right})"
        ).format(
            source=sql.SQL(_cypher_identifier(source)),
            target=sql.SQL(_cypher_identifier(target)),
            left=sql.Literal(left.value),
            right=sql.Literal(right.value),
        )
        for left, right in pairs
    )


@lru_cache(maxsize=16)
def _relation_predicate(source: str, target: str, edge: str) -> sql.Composable:
    """Constrains reads to permitted directed structural triples."""
    return sql.SQL(" OR ").join(
        sql.SQL("(label({edge}) = {relation} AND ({endpoints}))").format(
            edge=sql.SQL(_cypher_identifier(edge)),
            relation=sql.Literal(relation),
            endpoints=_endpoint_predicate(source, target, pairs),
        )
        for relation, pairs in RELATION_ENDPOINTS.items()
    )


def _permitted_relations(
    values: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Validates filters before issuing any graph query."""
    try:
        for value in values:
            relation_endpoints(value)
    except ValueError as exc:
        raise GraphOperationError("graph_contract", str(exc)) from exc
    return tuple(dict.fromkeys(values))


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

    def _vertex_params(self, vertex: GraphVertex) -> dict[str, JsonValue]:
        """Normalizes tenant constraints and properties for a graph vertex."""
        tenant_id = normalize_tenant_id(vertex.tenant_id)
        if not isinstance(vertex.vertex_id, str) or not vertex.vertex_id:
            raise GraphOperationError("graph_contract", "vertex ID is required")
        try:
            kind = GraphNodeKind(vertex.label)
        except ValueError as exc:
            raise GraphOperationError(
                "graph_contract", "unsupported structural kind"
            ) from exc
        props = vertex.properties.copy()
        if "ontology_ref" in props:
            raise GraphOperationError(
                "graph_contract", "ontology_ref requires a typed reference"
            )
        if vertex.ontology_ref is not None:
            reference = vertex.ontology_ref
            require_same_tenant(tenant_id, reference.tenant_id)
            if ONTOLOGY_KINDS.get(kind) != reference.resource_kind:
                raise GraphOperationError(
                    "graph_contract", "ontology resource kind is incompatible"
                )
            props["ontology_ref"] = reference.model_dump(mode="json")
        if "tenant_id" in props:
            require_same_tenant(tenant_id, props["tenant_id"])
        props["tenant_id"] = tenant_id
        props["id"] = vertex.vertex_id
        return props

    def _edge_params(self, edge: GraphEdge) -> dict[str, JsonValue]:
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
        self, conn: AsyncConnection[TupleRow], vertex: GraphVertex
    ) -> None:
        """Inserts or updates a vertex using an open connection transaction block."""
        props = self._vertex_params(vertex)
        # AGE has no uniqueness constraint spanning vertex labels. Serialize
        # identity checks so concurrent producers cannot change a vertex's role.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "jsonb_build_array(%s::text, %s::text, %s::text)::text, 0))",
            (self._graph_name, props["tenant_id"], vertex.vertex_id),
        )
        lookup = sql.SQL("""
            SELECT ag_catalog.agtype_to_jsonb(kind),
                   ag_catalog.agtype_to_jsonb(ontology_ref)
            FROM cypher({graph}, $$
                MATCH (v {{tenant_id: $tenant_id, id: $id}})
                RETURN label(v), v.ontology_ref
            $$, %s::agtype) AS (kind agtype, ontology_ref agtype)
        """).format(graph=sql.Literal(self._graph_name))
        existing = await conn.execute(
            lookup,
            (
                orjson.dumps(
                    {"tenant_id": props["tenant_id"], "id": vertex.vertex_id}
                ).decode(),
            ),
        )
        rows = await existing.fetchall()
        if len(rows) > 1 or (
            rows
            and (
                rows[0][0] != vertex.label
                or (
                    vertex.ontology_ref is not None
                    and rows[0][1] is not None
                    and rows[0][1] != props["ontology_ref"]
                )
            )
        ):
            logger.warning(
                "eskg_vertex_rejected", reason="identity_contract_conflict"
            )
            raise GraphOperationError(
                "graph_contract", "vertex kind or ontology revision conflict"
            )
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
        cursor = await conn.execute(query, (orjson.dumps(params).decode(),))
        if await cursor.fetchone() is None:
            raise GraphOperationError(
                "graph_contract", "vertex kind or ontology revision conflict"
            )

    async def ensure_vertex(self, vertex: GraphVertex) -> None:
        """Inserts or updates a vertex within a new transaction.

        Raises:
            GraphOperationError: If the execution fails.
        """
        try:
            async with self._client.tenant_connection(vertex.tenant_id) as conn:
                async with conn.transaction():
                    await self.ensure_vertex_on_connection(conn, vertex)
        except Exception as exc:
            raise GraphOperationError("ensure_vertex", str(exc)) from exc

    async def create_edge_on_connection(
        self, conn: AsyncConnection[TupleRow], edge: GraphEdge
    ) -> None:
        """Creates or updates a graph edge using an open connection transaction block."""
        props = self._edge_params(edge)
        try:
            pairs = relation_endpoints(edge.edge_type)
        except ValueError as exc:
            raise GraphOperationError("graph_contract", str(exc)) from exc
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
            WHERE {endpoints}
            MERGE (a)-[r:{edge_type}]->(b)
            {set_clause}
            RETURN r
        $$, %s::agtype) AS (r agtype)
        """).format(
            graph=sql.Literal(self._graph_name),
            edge_type=sql.SQL(_cypher_identifier(edge.edge_type)),
            set_clause=set_clause,
            endpoints=_endpoint_predicate("a", "b", pairs),
        )
        cursor = await conn.execute(query, (orjson.dumps(params).decode(),))
        if await cursor.fetchone() is None:
            raise GraphOperationError(
                "graph_contract", "missing or incompatible relation endpoints"
            )

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
        properties: dict[str, JsonValue],
        tenant_id: str,
    ) -> None:
        """Upserts tracking vertices and connects them with an influence edge relationship."""
        await self.ensure_metric(source_metric, tenant_id)
        await self.ensure_metric(target_metric, tenant_id)
        await self.create_edge(
            GraphEdge(
                source_vertex_id=source_metric,
                target_vertex_id=target_metric,
                edge_type="METRIC_INFLUENCE",
                tenant_id=tenant_id,
                properties=properties,
            )
        )

    async def upsert_causal_link(
        self,
        source_feature: str,
        target_feature: str,
        properties: dict[str, JsonValue],
        tenant_id: str,
    ) -> None:
        """Atomically versions one first-class CAUSES relationship and its variables."""
        inference_id = properties.get("inference_id")
        if not isinstance(inference_id, str) or not inference_id:
            raise GraphOperationError(
                "upsert_causal_link", "inference_id is required"
            )
        tenant_id_val = normalize_tenant_id(tenant_id)
        source = GraphVertex(
            vertex_id=source_feature,
            label="CausalVariable",
            tenant_id=tenant_id_val,
            properties={"name": source_feature},
        )
        target = GraphVertex(
            vertex_id=target_feature,
            label="CausalVariable",
            tenant_id=tenant_id_val,
            properties={"name": target_feature},
        )
        set_clause, set_params = _cypher_set_clause("r", properties)
        params = {
            "tenant_id": tenant_id_val,
            "source_id": source_feature,
            "target_id": target_feature,
            "inference_id": inference_id,
            **set_params,
        }
        query = sql.SQL("""
        SELECT * FROM cypher({graph}, $$
            MATCH (a:CausalVariable {{tenant_id: $tenant_id, id: $source_id}})
            MATCH (b:CausalVariable {{tenant_id: $tenant_id, id: $target_id}})
            MERGE (a)-[r:CAUSES {{
                tenant_id: $tenant_id,
                inference_id: $inference_id
            }}]->(b)
            {set_clause}
            RETURN r
        $$, %s::agtype) AS (r agtype)
        """).format(
            graph=sql.Literal(self._graph_name),
            set_clause=set_clause,
        )

        try:
            async with self._client.tenant_connection(tenant_id_val) as conn:
                async with conn.transaction():
                    await self.ensure_vertex_on_connection(conn, source)
                    await self.ensure_vertex_on_connection(conn, target)
                    await conn.execute(query, (orjson.dumps(params).decode(),))
        except Exception as exc:
            raise GraphOperationError("upsert_causal_link", str(exc)) from exc

    async def get_relationship_observations(
        self,
        vertex_ids: list[str],
        relationship_types: tuple[str, ...],
        tenant_id: str,
    ) -> tuple[tuple[str, str, str, dict[str, JsonValue]], ...]:
        """Loads structural evidence and derived edges as causal observation features."""
        if not vertex_ids or not relationship_types:
            return ()
        tenant_id_val = normalize_tenant_id(tenant_id)
        permitted = _permitted_relations(relationship_types)
        params = orjson.dumps(
            {
                "tenant_id": tenant_id_val,
                "vertex_ids": vertex_ids,
                "relationship_types": permitted,
            }
        ).decode()
        query = sql.SQL("""
        SELECT * FROM cypher({graph}, $$
            MATCH (a)-[r]->(b)
            WHERE a.tenant_id = $tenant_id
              AND b.tenant_id = $tenant_id
              AND a.id IN $vertex_ids
              AND b.id IN $vertex_ids
              AND r.tenant_id = $tenant_id
              AND label(r) IN $relationship_types
              AND ({contract})
            RETURN a.id, b.id, label(r), properties(r)
        $$, %s::agtype) AS (
            source_id agtype,
            target_id agtype,
            relationship_type agtype,
            properties agtype
        )
        """).format(
            graph=sql.Literal(self._graph_name),
            contract=_relation_predicate("a", "b", "r"),
        )

        try:
            async with self._client.tenant_connection(tenant_id_val) as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(query, (params,))
                    rows = await cursor.fetchall()
        except Exception as exc:
            raise GraphOperationError(
                "get_relationship_observations", str(exc)
            ) from exc

        observations: list[tuple[str, str, str, dict[str, JsonValue]]] = []
        for row in rows:
            if not row or len(row) < 4:
                continue
            raw_properties = row[3]
            if isinstance(raw_properties, dict):
                properties = raw_properties
            elif isinstance(raw_properties, str):
                try:
                    decoded = orjson.loads(raw_properties)
                    properties = decoded if isinstance(decoded, dict) else {}
                except orjson.JSONDecodeError:
                    properties = {}
            else:
                properties = {}
            observations.append(
                (
                    str(row[0]).strip('"'),
                    str(row[1]).strip('"'),
                    str(row[2]).strip('"'),
                    properties,
                )
            )
        return tuple(observations)

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

        permitted = _permitted_relations(relationship_types)
        k_min_val, k_max_val = int(k_min), int(k_max)
        max_vertices_val = int(max_vertices)
        if k_min_val < 1 or k_max_val < k_min_val or max_vertices_val < 1:
            raise GraphOperationError(
                "get_entity_k_hop_neighbors", "invalid traversal bounds"
            )
        tenant_id_val = normalize_tenant_id(tenant_id)
        try:
            async with self._client.tenant_connection(tenant_id_val) as conn:
                # AGE cannot alternate relationship labels in a variable-length pattern.
                query = sql.SQL("""
                      SELECT * FROM cypher({graph}, $$
                          MATCH (e)-[r]->(n)
                          WHERE e.tenant_id = $tenant_id
                            AND e.id IN $frontier_ids
                            AND n.tenant_id = $tenant_id
                            AND label(r) IN $relationship_types
                            AND r.tenant_id = $tenant_id
                            AND ({contract})
                            AND NOT (n.id IN $seen_ids)
                          RETURN DISTINCT n.id
                          UNION
                          MATCH (n)-[r]->(e)
                          WHERE e.tenant_id = $tenant_id
                            AND e.id IN $frontier_ids
                            AND n.tenant_id = $tenant_id
                            AND r.tenant_id = $tenant_id
                            AND label(r) IN $relationship_types
                            AND ({reverse_contract})
                            AND NOT (n.id IN $seen_ids)
                          RETURN DISTINCT n.id
                      $$, %s::agtype) AS (id agtype)
                      LIMIT {max_vertices}
                """).format(
                    graph=sql.Literal(self._graph_name),
                    max_vertices=sql.SQL(str(max_vertices_val)),
                    contract=_relation_predicate("e", "n", "r"),
                    reverse_contract=_relation_predicate("n", "e", "r"),
                )
                seen = {entity_id}
                visited_ids = [entity_id]
                frontier = [entity_id]
                out: list[str] = []
                async with conn.cursor() as cur:
                    for depth in range(1, k_max_val + 1):
                        if not frontier or len(out) >= max_vertices_val:
                            break
                        params = orjson.dumps(
                            {
                                "tenant_id": tenant_id_val,
                                "frontier_ids": frontier,
                                "seen_ids": visited_ids,
                                "relationship_types": permitted,
                            }
                        ).decode()
                        await cur.execute(query, (params,))
                        rows = await cur.fetchall()
                        next_frontier: list[str] = []
                        for row in rows:
                            if not row or row[0] is None:
                                continue
                            node_id = str(row[0]).strip('"')
                            if not node_id or node_id in seen:
                                continue
                            seen.add(node_id)
                            visited_ids.append(node_id)
                            next_frontier.append(node_id)
                            if depth >= k_min_val:
                                out.append(node_id)
                            if len(next_frontier) >= max_vertices_val:
                                break
                        frontier = next_frontier
        except Exception as exc:
            raise GraphOperationError(
                "get_entity_k_hop_neighbors", str(exc)
            ) from exc

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
        if not entity_ids or not relationship_types:
            return []

        permitted = _permitted_relations(relationship_types)
        max_events_val = int(max_events)
        if max_events_val < 1:
            raise GraphOperationError(
                "get_event_ids_for_entities", "invalid event limit"
            )
        tenant_id_val = normalize_tenant_id(tenant_id)
        params = orjson.dumps(
            {
                "entity_ids": entity_ids,
                "tenant_id": tenant_id_val,
                "relationship_types": permitted,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
            }
        ).decode()

        try:
            async with self._client.tenant_connection(tenant_id_val) as conn:
                query = sql.SQL("""
                      SELECT * FROM cypher({graph_name}, $$
                          UNWIND $entity_ids AS eid
                          MATCH (ent:Entity {{tenant_id: $tenant_id, id: eid}})-[r]->(ev:Event)
                          WHERE exists(ev.timestamp)
                            AND ev.tenant_id = $tenant_id
                            AND label(r) IN $relationship_types
                            AND r.tenant_id = $tenant_id
                            AND ({contract})
                            AND ev.timestamp >= $window_start
                            AND ev.timestamp <= $window_end
                        RETURN DISTINCT ev.id
                        UNION
                          UNWIND $entity_ids AS eid
                          MATCH (ev:Event)-[r]->(ent:Entity {{tenant_id: $tenant_id, id: eid}})
                          WHERE ev.tenant_id = $tenant_id
                            AND r.tenant_id = $tenant_id
                            AND label(r) IN $relationship_types
                            AND ({reverse_contract})
                            AND ev.timestamp >= $window_start
                            AND ev.timestamp <= $window_end
                          RETURN DISTINCT ev.id
                    $$, %s::agtype) AS (id agtype)
                    LIMIT {max_events}
                """).format(
                    graph_name=sql.Literal(self._graph_name),
                    max_events=sql.SQL(str(max_events_val)),
                    contract=_relation_predicate("ent", "ev", "r"),
                    reverse_contract=_relation_predicate("ev", "ent", "r"),
                )
                async with conn.cursor() as cur:
                    await cur.execute(query, (params,))
                    rows = await cur.fetchall()
        except Exception as exc:
            raise GraphOperationError(
                "get_event_ids_for_entities", str(exc)
            ) from exc

        return [
            str(row[0]).strip('"') for row in rows if row and row[0] is not None
        ]

    async def insert_event_on_connection(
        self, conn: AsyncConnection[TupleRow], event: EventRecord
    ) -> None:
        """Inserts an event vertex and log table record using an active connection transaction."""
        tenant_id = normalize_tenant_id(event.tenant_id)
        props = event.properties.copy()
        if "tenant_id" in props:
            require_same_tenant(tenant_id, props["tenant_id"])
        props["tenant_id"] = tenant_id
        props["timestamp"] = event.timestamp.isoformat()
        props["event_type"] = event.event_type.value
        if event.location_coords:
            location: list[JsonValue] = list(event.location_coords)
            props["location"] = location

        await self.ensure_vertex_on_connection(
            conn,
            GraphVertex(
                vertex_id=event.event_id,
                label=GraphNodeKind.EVENT,
                tenant_id=tenant_id,
                properties=props,
                ontology_ref=event.ontology_ref,
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
        properties: dict[str, JsonValue] | None = None,
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
        conn: AsyncConnection[TupleRow],
        *,
        vertex: GraphVertex,
        event: EventRecord,
        edge_type: str,
        state_type: str,
        state_value: dict[str, JsonValue],
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
        self, conn: AsyncConnection[TupleRow], state: EntityStateRecord
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
        conn: AsyncConnection[TupleRow],
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
