"""Validates structural mutations and versioned provenance on real Apache AGE."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from galadril_ontology import ResourceKind
from galadril_vision.common.config import PostgresConnectorConfig
from galadril_vision.common.eskg import OntologyReference
from galadril_vision.common.exceptions import GraphOperationError
from galadril_vision.common.types import (
    EventRecord,
    EventType,
    GraphEdge,
    GraphVertex,
)
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import GraphStore
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_eskg_structural_contract_on_age() -> None:
    container = PostgresContainer(
        "ghcr.io/aeluin-technologies/galadril-database:latest",
        username="postgres",
        password="postgres",
        dbname="postgres",
        driver=None,
    ).with_command(
        "postgres -c listen_addresses=* "
        "-c shared_preload_libraries=timescaledb,age,pg_cron,pg_stat_statements,pg_wait_sampling"
    )
    await asyncio.to_thread(container.start)
    config = PostgresConnectorConfig(
        database="postgres",
        host=f"{container.get_container_host_ip()}:{container.get_exposed_port(5432)}",
        user="postgres",
        password="postgres",
        graph_name="eskg_contract",
        min_connections=1,
        max_connections=2,
    )
    client = PostgresClient(config)
    try:
        await client.connect(initialize_database_infrastructure=True)
        store = GraphStore(client, config)
        reference = OntologyReference(
            tenant_id="tenant-a",
            ontology_id="operations",
            revision_id="a" * 32,
            resource_id="object.person",
            resource_kind=ResourceKind.OBJECT_TYPE,
        )
        entity = GraphVertex(
            "evt_entity", "Entity", "tenant-a", ontology_ref=reference
        )
        event = EventRecord(
            event_id="opaque-event",
            tenant_id="tenant-a",
            event_type=EventType.TRANSACTION,
            timestamp=datetime(2026, 10, 5, tzinfo=UTC),
        )
        await store.ensure_vertex(entity)
        await store.ensure_vertex(entity)
        await store.insert_event(event)
        concurrent = await asyncio.gather(
            store.ensure_vertex(
                GraphVertex("concurrent", "Entity", "tenant-a")
            ),
            store.ensure_vertex(GraphVertex("concurrent", "Event", "tenant-a")),
            return_exceptions=True,
        )
        assert sum(result is None for result in concurrent) == 1
        assert (
            sum(
                isinstance(result, GraphOperationError) for result in concurrent
            )
            == 1
        )
        await store.create_edge(
            GraphEdge("evt_entity", "opaque-event", "DERIVED_FROM", "tenant-a")
        )
        await store.create_edge(
            GraphEdge("opaque-event", "evt_entity", "OCCUR", "tenant-a")
        )
        with pytest.raises(GraphOperationError, match="endpoints"):
            await store.create_edge(
                GraphEdge("evt_entity", "opaque-event", "TRIGGERS", "tenant-a")
            )
        with pytest.raises(GraphOperationError, match="endpoints"):
            await store.create_edge(
                GraphEdge(
                    "opaque-event", "evt_entity", "DERIVED_FROM", "tenant-a"
                )
            )
        with pytest.raises(GraphOperationError, match="endpoints"):
            await store.create_edge(
                GraphEdge("evt_entity", "missing", "DERIVED_FROM", "tenant-a")
            )
        with pytest.raises(GraphOperationError, match="conflict"):
            await store.ensure_vertex(
                replace(entity, label="State", ontology_ref=None)
            )
        with pytest.raises(GraphOperationError, match="conflict"):
            await store.ensure_vertex(
                replace(
                    entity,
                    ontology_ref=reference.model_copy(
                        update={"revision_id": "b" * 32}
                    ),
                )
            )
        assert await store.get_event_ids_for_entities(
            [entity.vertex_id],
            event.timestamp - timedelta(seconds=1),
            event.timestamp + timedelta(seconds=1),
            10,
            ("OCCUR",),
            "tenant-a",
        ) == ["opaque-event"]
        assert await store.get_entity_k_hop_neighbors(
            entity.vertex_id,
            1,
            1,
            10,
            ["OCCUR"],
            "tenant-a",
        ) == ["opaque-event"]
        assert (
            await store.get_entity_k_hop_neighbors(
                entity.vertex_id,
                1,
                1,
                10,
                ["OCCUR"],
                "tenant-b",
            )
            == []
        )
        observations = await store.get_relationship_observations(
            [entity.vertex_id, event.event_id],
            ("DERIVED_FROM", "OCCUR"),
            "tenant-a",
        )
        assert {
            (source, target, relation)
            for source, target, relation, _ in observations
        } == {
            (entity.vertex_id, event.event_id, "DERIVED_FROM"),
            (event.event_id, entity.vertex_id, "OCCUR"),
        }
        assert all(
            properties == {"tenant_id": "tenant-a"}
            for _, _, _, properties in observations
        )
    finally:
        await client.close()
        await asyncio.to_thread(container.stop)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
