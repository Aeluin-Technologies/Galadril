"""Unit tests for asynchronous database pipelines, states, and graph drivers."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_vision.compute.tasks import (
    PostgresRuntimeState,
    _clone_postgres_config,
    _upsert_identity_link,
    _vector_concurrency_limit,
    get_pg_stores,
    resolve_entities_batch,
    sink_to_db_batch,
)
from galadril_vision.connectors.postgres.vector import IdentityCandidate


class TestPostgresRuntimeState:
    """Verifies dataclass attribute initialization across client environments."""

    def test_slots_and_defaults(self) -> None:
        """Validates property slot arrays isolate runtime handles cleanly."""
        state = PostgresRuntimeState()
        assert state.client is None
        assert state.vector_store is None
        assert state.graph_store is None
        assert state.init_lock is None


class TestTasksDatabasePipelines:
    """Systematically evaluates connections, identity lookups, and graph mutation pipelines."""

    def test_clone_postgres_config_pydantic_branch(self) -> None:
        """Tests cloning logic when processing configuration objects."""
        cfg = MagicMock()
        cfg.min_connections = 0
        cfg.max_connections = 0
        cfg.model_copy = MagicMock(return_value="pydantic_cloned_copy")

        res = _clone_postgres_config(cfg)
        assert res == "pydantic_cloned_copy"
        cfg.model_copy.assert_called_once()

    def test_clone_postgres_config_dict_branch(self) -> None:
        """Tests cloning properties out of standard parameter dictionary contexts."""
        cfg = {"min_connections": 2, "max_connections": 1}
        res = _clone_postgres_config(cfg)
        assert res["min_connections"] == 2
        assert res["max_connections"] == 2

    def test_clone_postgres_config_object_branch(self) -> None:
        """Tests attribute configuration mutations on generic system classes."""

        class CustomConfig:
            min_connections = 5
            max_connections = 2

        res = _clone_postgres_config(CustomConfig())
        assert res.min_connections == 5
        assert res.max_connections == 5

    @pytest.mark.asyncio
    async def test_get_pg_stores_cached_return(self) -> None:
        """Ensures active database links return directly from internal state caches."""
        state = PostgresRuntimeState(
            client="c",  # type: ignore
            vector_store="v",  # type: ignore
            graph_store="g",  # type: ignore
        )
        c, v, g = await get_pg_stores(MagicMock(), state)
        assert (c, v, g) == ("c", "v", "g")

    @pytest.mark.asyncio
    async def test_get_pg_stores_initialization_flow(self) -> None:
        """Validates pool limits settings when initializing connection pools."""
        state = PostgresRuntimeState()
        cfg = {"min_connections": 1, "max_connections": 2}

        with (
            patch(
                "galadril_vision.compute.tasks.PostgresClient"
            ) as mock_client_cls,
            patch("galadril_vision.compute.tasks.VectorStore") as mock_v_cls,
            patch("galadril_vision.compute.tasks.GraphStore") as mock_g_cls,
        ):
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_v_cls.return_value = "v_store"
            mock_g_cls.return_value = "g_store"

            c, v, g = await get_pg_stores(cfg, state)
            assert c == mock_client
            assert v == "v_store"
            assert g == "g_store"
            mock_client.connect.assert_called_once_with(
                initialize_database_infrastructure=False
            )

    @pytest.mark.asyncio
    async def test_get_pg_stores_exception_handling(self) -> None:
        """Verifies that pool setup errors reset state handles before propagating."""
        state = PostgresRuntimeState()
        with patch(
            "galadril_vision.compute.tasks.PostgresClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.connect.side_effect = RuntimeError(
                "Pool connection refused"
            )
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="Pool connection refused"):
                await get_pg_stores({}, state)

            assert state.client is None

    def test_vector_concurrency_limit(self) -> None:
        """Validates execution slots calculations against max pool constraints."""
        cfg = MagicMock()
        cfg.max_connections = 10
        assert _vector_concurrency_limit(cfg, 5) == 5
        assert _vector_concurrency_limit(cfg, 20) == 10

    @pytest.mark.asyncio
    async def test_identity_link_rejects_identifier_remapping(self) -> None:
        """Enforces immutable PostgreSQL to LI-ESKG identity correspondence."""
        conn = MagicMock()
        conn.execute = AsyncMock()
        cursor = AsyncMock()
        conn.execute.return_value = cursor
        cursor.fetchone.return_value = (42,)

        await _upsert_identity_link(
            conn,
            tenant_id="tenant-1",
            entity_id="person-1",
            licorne_identity_id=42,
            licorne_version=7,
        )

        cursor.fetchone.return_value = None
        with pytest.raises(RuntimeError, match="different LI-ESKG identity"):
            await _upsert_identity_link(
                conn,
                tenant_id="tenant-1",
                entity_id="person-1",
                licorne_identity_id=43,
                licorne_version=8,
            )

    @pytest.mark.asyncio
    async def test_resolve_entities_batch(self) -> None:
        """Evaluates resolution routing when executing vector embedding similarity searches."""
        state = PostgresRuntimeState()
        cfg = MagicMock()
        cfg.vector_dimensions = 1024
        cfg.vector_search_timeout_ms = 5000

        mock_v_store = AsyncMock()
        mock_v_store.find_resolution_candidates = AsyncMock(
            return_value=[IdentityCandidate("ent_abc", 0.95, "face")]
        )

        inference_results = [
            {"error": "skip_me"},
            {
                "prediction": {
                    "faces": [{"embedding": [0.1] * 1024, "model_name": "face"}]
                },
                "model_name": "m",
            },
        ]
        tenant_ids = ["acme", "acme"]

        with patch(
            "galadril_vision.compute.tasks.get_pg_stores",
            return_value=(MagicMock(), mock_v_store, MagicMock()),
        ):
            res = await resolve_entities_batch(
                state=state,
                postgres_config=cfg,
                inference_results=inference_results,
                tenant_ids=tenant_ids,
                modality="f",
                threshold=0.8,
            )
            assert len(res) == 2
            assert res[0] == []
            assert res[1][0]["resolved_entity_id"] == "ent_abc"
            assert res[1][0]["is_unknown"] is False

    @pytest.mark.asyncio
    async def test_resolve_entities_batch_timeouts_and_unknowns(self) -> None:
        """Ensures search timeouts gracefully fallback to unmapped entity tracking categories."""
        state = PostgresRuntimeState()
        cfg = MagicMock()
        mock_v_store = AsyncMock()
        mock_v_store.find_resolution_candidates.side_effect = TimeoutError()

        inference_results = [
            {
                "prediction": {
                    "faces": [{"embedding": [0.1], "model_name": "face"}]
                }
            }
        ]

        with patch(
            "galadril_vision.compute.tasks.get_pg_stores",
            return_value=(MagicMock(), mock_v_store, MagicMock()),
        ):
            with pytest.raises(RuntimeError, match="refusing to create"):
                await resolve_entities_batch(
                    state=state,
                    postgres_config=cfg,
                    inference_results=inference_results,
                    tenant_ids=["acme"],
                    modality="f",
                    threshold=0.8,
                )

    @pytest.mark.asyncio
    async def test_sink_to_db_batch(self) -> None:
        """Validates standard insert mutations on property graph drivers."""
        state = PostgresRuntimeState()
        cfg = MagicMock()
        cfg.vector_dimensions = 1024

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock()
        mock_tx = MagicMock()
        mock_conn.transaction.return_value = mock_tx

        mock_client = MagicMock()
        mock_client.tenant_connection.return_value.__aenter__.return_value = (
            mock_conn
        )

        mock_v_store = AsyncMock()
        mock_g_store = AsyncMock()
        mock_g_store.prepare_connection = AsyncMock()
        mock_g_store.insert_event_on_connection = AsyncMock()
        mock_g_store.ensure_vertex_on_connection = AsyncMock()
        mock_g_store.create_edge_on_connection = AsyncMock()

        resolved_items = [
            [
                {
                    "resolved_entity_id": "ent_1",
                    "embedding": [0.1] * 1024,
                    "entity_type": "User",
                }
            ]
        ]
        record_ids = ["rec_1"]
        sources = ["s3"]
        tenant_ids = ["acme"]
        event_types = ["OBSERVATION"]

        # Keep the fixture explicit to satisfy invariant collection typing.
        raw_payloads: list[dict[str, Any] | None] = [
            {
                "authz": {
                    "tuples": [
                        {
                            "resource": "raw:acme/source/object",
                            "relation": "parent",
                            "subject": "tenant:acme",
                        }
                    ],
                    "requested_resource": "raw:acme/source/object",
                }
            }
        ]

        with patch(
            "galadril_vision.compute.tasks.get_pg_stores",
            return_value=(mock_client, mock_v_store, mock_g_store),
        ):
            res = await sink_to_db_batch(
                state=state,
                postgres_config=cfg,
                resolved_items=resolved_items,
                record_ids=record_ids,
                sources=sources,
                tenant_ids=tenant_ids,
                event_types=event_types,
                raw_payloads=raw_payloads,
                entity_type="E",
                modality="m",
                edge_type="EDGE",
                state_type="s",
            )
            assert res == [True]
            mock_g_store.insert_event_on_connection.assert_called_once()
            mock_g_store.ensure_vertex_on_connection.assert_called_once()
            mock_g_store.create_edge_on_connection.assert_called_once()
            mock_conn.execute.assert_called_once()
