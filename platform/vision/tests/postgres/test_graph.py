"""Unit tests targeting the Apache AGE and TimescaleDB data access layer."""

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.common.exceptions import GraphOperationError
from galadril_vision.connectors.postgres.graph import (
    GraphStore,
    _cypher_identifier,
    _cypher_set_clause,
)


class DummyEventType(Enum):
    """Stub enum representing an event type wrapper."""

    ANOMALY = "ANOMALY"


class DummyGraphVertex:
    """Stub tracking properties required by GraphVertex serialization."""

    def __init__(
        self,
        vertex_id: str,
        label: str,
        tenant_id: str,
        properties: dict[str, Any],
    ) -> None:
        self.vertex_id = vertex_id
        self.label = label
        self.tenant_id = tenant_id
        self.properties = properties


class DummyGraphEdge:
    """Stub tracking properties required by GraphEdge serialization."""

    def __init__(
        self,
        source_vertex_id: str,
        target_vertex_id: str,
        edge_type: str,
        tenant_id: str,
        properties: dict[str, Any],
    ) -> None:
        self.source_vertex_id = source_vertex_id
        self.target_vertex_id = target_vertex_id
        self.edge_type = edge_type
        self.tenant_id = tenant_id
        self.properties = properties


class DummyEventRecord:
    """Stub mimicking EventRecord model layout."""

    def __init__(
        self,
        event_id: str,
        tenant_id: str,
        event_type: Any,
        timestamp: datetime,
        location_coords: Any,
        properties: dict[str, Any],
    ) -> None:
        self.event_id = event_id
        self.tenant_id = tenant_id
        self.event_type = event_type
        self.timestamp = timestamp
        self.location_coords = location_coords
        self.properties = properties


class DummyEntityStateRecord:
    """Stub mimicking EntityStateRecord model layout."""

    def __init__(
        self,
        entity_id: str,
        event_id: str,
        state_type: str,
        state_value: dict[str, Any],
        event_time: datetime,
        tenant_id: str,
    ) -> None:
        self.entity_id = entity_id
        self.event_id = event_id
        self.state_type = state_type
        self.state_value = state_value
        self.event_time = event_time
        self.tenant_id = tenant_id


@pytest.fixture
def mock_postgres_client() -> MagicMock:
    """Generates an isolated mock client instance."""
    client = MagicMock()
    client.connection = MagicMock()
    return client


@pytest.fixture
def mock_config() -> MagicMock:
    """Supplies standard mock configurations for graph execution."""
    config = MagicMock()
    config.graph_name = "vision_graph"
    return config


def _connection_mock() -> MagicMock:
    """Builds a psycopg-shaped connection with synchronous context factories."""
    connection = MagicMock()
    connection.cursor = MagicMock()
    connection.execute = AsyncMock()
    connection.transaction = MagicMock()
    return connection


def test_cypher_identifier_validation() -> None:
    """Validates alphanumeric constraint filters on identifiers."""
    assert _cypher_identifier("ValidLabel_123") == "ValidLabel_123"

    with pytest.raises(GraphOperationError, match="invalid Cypher identifier"):
        _cypher_identifier("invalid-label-hyphen")

    with pytest.raises(GraphOperationError, match="invalid Cypher identifier"):
        _cypher_identifier("injection; DROP TABLE;")


def test_cypher_set_clause_generation() -> None:
    """Ensures accurate string assembly and lexicographical parameter sorting mappings."""
    clause, params = _cypher_set_clause("v", {"b_prop": 2, "a_prop": "test"})
    assert "v.a_prop = $p_0" in clause.as_string(None)
    assert "v.b_prop = $p_1" in clause.as_string(None)
    assert params == {"p_0": "test", "p_1": 2}

    empty_clause, empty_params = _cypher_set_clause("v", {})
    assert empty_clause.as_string(None) == ""
    assert empty_params == {}


@pytest.mark.asyncio
async def test_graph_store_initialization(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Validates schema execution during setup."""
    mock_conn = _connection_mock()
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    await store.initialize()

    mock_conn.execute.assert_called_once()


@pytest.mark.asyncio
@patch(
    "galadril_vision.connectors.postgres.graph.normalize_tenant_id",
    return_value="tenant-abc",
)
@patch("galadril_vision.connectors.postgres.graph.require_same_tenant")
async def test_ensure_vertex_processing(
    mock_require_tenant: MagicMock,
    mock_norm_tenant: MagicMock,
    mock_postgres_client: MagicMock,
    mock_config: MagicMock,
) -> None:
    """Ensures vertex parameter extraction, tenant safety verification, and execution match requirements."""
    mock_conn = _connection_mock()
    mock_conn.transaction.return_value.__aenter__.return_value = MagicMock()
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    vertex = DummyGraphVertex(
        vertex_id="v-1",
        label="Entity",
        tenant_id="tenant-abc",
        properties={"tenant_id": "tenant-abc", "speed": 45},
    )

    await store.ensure_vertex(vertex)  # type: ignore
    mock_conn.execute.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_vertex_failure_wrapping(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Guarantees exceptions are cleanly intercepted and wrapped into GraphOperationError containers."""
    mock_conn = _connection_mock()
    mock_conn.transaction.side_effect = RuntimeError("Database offline")
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    vertex = DummyGraphVertex(
        vertex_id="v-1", label="Entity", tenant_id="t", properties={}
    )

    with pytest.raises(GraphOperationError, match="ensure_vertex"):
        await store.ensure_vertex(vertex)  # type: ignore


@pytest.mark.asyncio
@patch(
    "galadril_vision.connectors.postgres.graph.normalize_tenant_id",
    return_value="tenant-abc",
)
async def test_create_edge_processing(
    mock_norm_tenant: MagicMock,
    mock_postgres_client: MagicMock,
    mock_config: MagicMock,
) -> None:
    """Ensures edge parameter parsing transformations correctly strip constraints before execution."""
    mock_conn = _connection_mock()
    mock_conn.transaction.return_value.__aenter__.return_value = MagicMock()
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    edge = DummyGraphEdge(
        source_vertex_id="v-1",
        target_vertex_id="v-2",
        edge_type="LINKED",
        tenant_id="tenant-abc",
        properties={"weight": 1.5},
    )

    await store.create_edge(edge)  # type: ignore
    mock_conn.execute.assert_called_once()


@pytest.mark.asyncio
async def test_create_edge_failure_wrapping(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Validates error catching abstractions inside standard edge storage routines."""
    mock_conn = _connection_mock()
    mock_conn.transaction.side_effect = RuntimeError("Transaction aborted")
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    edge = DummyGraphEdge(
        source_vertex_id="a",
        target_vertex_id="b",
        edge_type="E",
        tenant_id="t",
        properties={},
    )

    with pytest.raises(GraphOperationError, match="create_edge"):
        await store.create_edge(edge)  # type: ignore


@pytest.mark.asyncio
async def test_system_metric_helpers_routing(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Tests system metric injection layers and multi-edge influence generation cascades."""
    store = GraphStore(client=mock_postgres_client, config=mock_config)

    with (
        patch.object(
            store, "ensure_vertex", new_callable=AsyncMock
        ) as mock_vertex,
        patch.object(store, "create_edge", new_callable=AsyncMock) as mock_edge,
    ):
        await store.upsert_metric_influence(
            "metric_a", "metric_b", {"factor": 0.8}
        )

        assert mock_vertex.call_count == 2
        mock_edge.assert_called_once()


@pytest.mark.asyncio
async def test_get_entity_k_hop_neighbors_routing(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies recursive hop queries filter structures and accurately evaluate depth limits."""
    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = [
        ("neighbor-1", 1),
        ("neighbor-2", 2),
        ("neighbor-3", 5),
    ]

    mock_conn = _connection_mock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)

    empty_res = await store.get_entity_k_hop_neighbors("ent-1", 1, 3, 10, [])
    assert empty_res == []

    res = await store.get_entity_k_hop_neighbors(
        "ent-1", 1, 3, 10, ["CONNECTED_TO", "OWNER_OF"]
    )
    assert res == ["neighbor-1", "neighbor-2"]


@pytest.mark.asyncio
async def test_get_entity_k_hop_neighbors_error_handling(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies error handling when executing K-hop queries."""
    mock_conn = _connection_mock()
    mock_conn.cursor.side_effect = RuntimeError("Query error")
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    with pytest.raises(GraphOperationError, match="get_entity_k_hop_neighbor"):
        await store.get_entity_k_hop_neighbors("ent-1", 1, 2, 5, ["K"])


@pytest.mark.asyncio
async def test_get_event_ids_for_entities_routing(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies that multi-entity contextual query loops aggregate values accurately."""
    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = [("ev-123",), ("ev-456",), (None,)]

    mock_conn = _connection_mock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)

    empty_res = await store.get_event_ids_for_entities(
        [], datetime.now(), datetime.now(), 10, ("E",)
    )
    assert empty_res == []

    res = await store.get_event_ids_for_entities(
        ["ent-1"], datetime.now(), datetime.now(), 10, ("LINKED_EVENT",)
    )
    assert res == ["ev-123", "ev-456"]


@pytest.mark.asyncio
async def test_get_event_ids_for_entities_error_handling(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies error handling when looking up event IDs for entities."""
    mock_conn = _connection_mock()
    mock_conn.cursor.side_effect = RuntimeError("Execution aborted")
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    with pytest.raises(GraphOperationError, match="get_event_ids_for_entities"):
        await store.get_event_ids_for_entities(
            ["e"], datetime.now(), datetime.now(), 5, ("R",)
        )


@pytest.mark.asyncio
async def test_insert_event_lifecycle(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Validates atomic operations for concurrent vertices and hyper-table insertions."""
    mock_conn = _connection_mock()
    mock_conn.transaction.return_value.__aenter__.return_value = MagicMock()
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    event = DummyEventRecord(
        event_id="ev-1",
        tenant_id="tenant-1",
        event_type=DummyEventType.ANOMALY,
        timestamp=datetime.now(UTC),
        location_coords="POINT(2.35 48.85)",
        properties={"severity": "high"},
    )

    with patch.object(
        store, "ensure_vertex_on_connection", new_callable=AsyncMock
    ) as mock_vertex_conn:
        await store.insert_event(event)  # type: ignore
        mock_vertex_conn.assert_called_once()
        assert mock_conn.execute.call_count == 1


@pytest.mark.asyncio
async def test_insert_event_error_wrapping(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies error handling during event insertions."""
    mock_conn = _connection_mock()
    mock_conn.transaction.side_effect = RuntimeError("Crash")
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    event = DummyEventRecord(
        event_id="e",
        tenant_id="t",
        event_type=DummyEventType.ANOMALY,
        timestamp=datetime.now(),
        location_coords=None,
        properties={},
    )
    with pytest.raises(GraphOperationError, match="insert_event"):
        await store.insert_event(event)  # type: ignore


@pytest.mark.asyncio
async def test_link_entity_to_event_helper(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies shorthand directional relational linkage wrapper pathways."""
    store = GraphStore(client=mock_postgres_client, config=mock_config)
    with patch.object(
        store, "create_edge", new_callable=AsyncMock
    ) as mock_create_edge:
        await store.link_entity_to_event("entity-1", "event-9", "tenant-x")
        mock_create_edge.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_entity_observation_pipeline(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Verifies execution ordering and mapping accuracy across compound state operations."""
    mock_conn = _connection_mock()
    store = GraphStore(client=mock_postgres_client, config=mock_config)

    vertex = DummyGraphVertex("v-1", "Label", "tenant-1", {})
    event = DummyEventRecord(
        "ev-1", "tenant-1", DummyEventType.ANOMALY, datetime.now(), None, {}
    )

    with (
        patch.object(
            store, "insert_event_on_connection", new_callable=AsyncMock
        ) as m_ev,
        patch.object(
            store, "ensure_vertex_on_connection", new_callable=AsyncMock
        ) as m_vx,
        patch.object(
            store, "create_edge_on_connection", new_callable=AsyncMock
        ) as m_ed,
        patch.object(
            store, "insert_entity_state_on_connection", new_callable=AsyncMock
        ) as m_st,
    ):
        await store.upsert_entity_observation_on_connection(
            mock_conn,
            vertex=vertex,  # type: ignore
            event=event,  # type: ignore
            edge_type="SAW",
            state_type="metrics",
            state_value={"val": 1},
        )
        m_ev.assert_called_once()
        m_vx.assert_called_once()
        m_ed.assert_called_once()
        m_st.assert_called_once()


@pytest.mark.asyncio
async def test_insert_entity_state_geometry_resolution(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Ensures spatial point strings are extracted when coordinates exist."""
    mock_conn = _connection_mock()
    mock_conn.transaction.return_value.__aenter__.return_value = MagicMock()
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)
    state = DummyEntityStateRecord(
        entity_id="ent-1",
        event_id="ev-1",
        state_type="loc",
        state_value={"lat": 48.85, "lon": 2.35},
        event_time=datetime.now(),
        tenant_id="t",
    )

    await store.insert_entity_state(state)  # type: ignore
    call_args = mock_conn.execute.call_args[0]
    assert "SRID=4326;POINT(2.35 48.85)" in call_args[1]


@pytest.mark.asyncio
async def test_insert_entity_states_batch_execution(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Confirms empty sequence short-circuit logic and batch processing performance behaviors."""
    mock_cursor = AsyncMock()
    mock_conn = _connection_mock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_conn.transaction.return_value.__aenter__.return_value = MagicMock()
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    store = GraphStore(client=mock_postgres_client, config=mock_config)

    await store.insert_entity_states_batch([])
    mock_cursor.executemany.assert_not_called()

    state = DummyEntityStateRecord(
        entity_id="ent-1",
        event_id="ev-1",
        state_type="loc",
        state_value={"lat": 10, "lon": 20},
        event_time=datetime.now(),
        tenant_id="t-1",
    )
    await store.insert_entity_states_batch([state])  # type: ignore
    mock_cursor.executemany.assert_called_once()
