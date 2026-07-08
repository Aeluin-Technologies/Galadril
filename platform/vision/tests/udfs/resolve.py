"""Unit tests for entity resolution via Postgres vector search."""

from unittest.mock import MagicMock, patch

import pytest
from daft import Series
from galadril_vision.compute.udfs.resolve import (
    _get_postgres_state,
    resolve_entities_udf,
)


class TestResolveEntitiesUdf:
    """Validates local database routing behaviors inside data pipelines."""

    def test_get_postgres_state_caching(self) -> None:
        """Verifies state structures persist across execution layers on local threads."""
        state_one = _get_postgres_state()
        state_two = _get_postgres_state()
        assert state_one == state_two
        assert state_one.__class__.__name__ == "PostgresRuntimeState"

    def test_resolve_entities_udf_success(self) -> None:
        """Validates integration pipelines loop and map downstream task lists."""
        inference_results = Series.from_pylist([[{"prediction": "val"}]])
        tenant_ids = Series.from_pylist(["acme"])
        config = MagicMock()
        config.vector_search_timeout_ms = 5000

        mock_batch_result = [[{"resolved_entity_id": "ent_1"}]]

        with patch(
            "galadril_vision.compute.udfs.resolve.resolve_entities_batch",
            return_value=mock_batch_result,
        ) as mock_batch:
            res = resolve_entities_udf(
                inference_results,
                tenant_ids,
                postgres_config=config,
                modality="face_recognition",
                threshold=0.8,
            )
            assert res == mock_batch_result
            mock_batch.assert_called_once()

    def test_resolve_entities_udf_inner_async_failure(self) -> None:
        """Ensures exceptions from underlying layers bubbles up correctly through sync gates."""
        inference_results = Series.from_pylist([[{"prediction": "val"}]])
        tenant_ids = Series.from_pylist(["acme"])
        config = MagicMock()

        with patch(
            "galadril_vision.compute.udfs.resolve.resolve_entities_batch",
            side_effect=RuntimeError("DB Offline"),
        ):
            with pytest.raises(RuntimeError, match="DB Offline"):
                resolve_entities_udf(
                    inference_results, tenant_ids, postgres_config=config
                )

    def test_resolve_entities_udf_outer_loop_recreation(self) -> None:
        """Ensures event loop structures re-initialize cleanly when closed."""
        inference_results = Series.from_pylist([[]])
        tenant_ids = Series.from_pylist(["acme"])
        config = MagicMock()

        with patch(
            "galadril_vision.compute.udfs.resolve._THREAD_LOCAL"
        ) as mock_tl:
            mock_loop = MagicMock()
            mock_loop.is_closed.return_value = True
            mock_tl.event_loop = mock_loop

            with patch(
                "galadril_vision.compute.udfs.resolve.asyncio.new_event_loop"
            ) as mock_new_loop:
                mock_created = MagicMock()
                mock_new_loop.return_value = mock_created
                mock_created.run_until_complete.side_effect = ValueError(
                    "Loop Run Error"
                )

                with pytest.raises(ValueError, match="Loop Run Error"):
                    resolve_entities_udf(
                        inference_results, tenant_ids, postgres_config=config
                    )
                mock_new_loop.assert_called_once()
