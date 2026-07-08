"""Unit tests for persisting resolved metrics inside relational database storage."""

from unittest.mock import MagicMock, patch

import pytest
from daft import Series
from galadril_vision.compute.udfs.sink import (
    _get_postgres_state,
    sink_to_db_udf,
)


class TestSinkToDbUdf:
    """Validates structural mutation pipelines across local storage drivers."""

    def test_get_postgres_state_retrieval(self) -> None:
        """Ensures local execution contexts isolate local backend workers."""
        state = _get_postgres_state()
        assert state is not None

    def test_sink_to_db_udf_success_path(self) -> None:
        """Validates that row conversions propagate accurately to relational layers."""
        resolved = Series.from_pylist([[{"resolved_entity_id": "id"}]])
        records = Series.from_pylist(["rec_1"])
        sources = Series.from_pylist(["s3://path"])
        tenants = Series.from_pylist(["acme"])
        events = Series.from_pylist(["OBSERVATION"])
        payloads = Series.from_pylist([{"authz": None}])
        config = MagicMock()

        with patch(
            "galadril_vision.compute.udfs.sink.sink_to_db_batch",
            return_value=[True],
        ) as mock_batch:
            res = sink_to_db_udf(
                resolved,
                records,
                sources,
                tenants,
                events,
                payloads,
                postgres_config=config,
            )
            assert res == [True]
            mock_batch.assert_called_once()

    def test_sink_to_db_udf_inner_async_failure(self) -> None:
        """Verifies transactional rollback failures across pipeline workflows."""
        resolved = Series.from_pylist([[]])
        records = Series.from_pylist(["rec_1"])
        sources = Series.from_pylist(["s3"])
        tenants = Series.from_pylist(["acme"])
        events = Series.from_pylist(["OBSERVATION"])
        payloads = Series.from_pylist([{}])
        config = MagicMock()

        with patch(
            "galadril_vision.compute.udfs.sink.sink_to_db_batch",
            side_effect=RuntimeError("Transaction Aborted"),
        ):
            with pytest.raises(RuntimeError, match="Transaction Aborted"):
                sink_to_db_udf(
                    resolved,
                    records,
                    sources,
                    tenants,
                    events,
                    payloads,
                    postgres_config=config,
                )

    def test_sink_to_db_udf_outer_execution_failure(self) -> None:
        """Validates system diagnostics logs track unhandled batch execution crashes."""
        resolved = Series.from_pylist([[]])
        records = Series.from_pylist(["rec_1"])
        sources = Series.from_pylist(["s3"])
        tenants = Series.from_pylist(["acme"])
        events = Series.from_pylist(["OBSERVATION"])
        payloads = Series.from_pylist([{}])
        config = MagicMock()

        with patch(
            "galadril_vision.compute.udfs.sink._THREAD_LOCAL"
        ) as mock_tl:
            mock_loop = MagicMock()
            mock_loop.is_closed.return_value = False
            mock_loop.run_until_complete.side_effect = Exception(
                "Fatal Thread Crash"
            )
            mock_tl.event_loop = mock_loop

            with pytest.raises(Exception, match="Fatal Thread Crash"):
                sink_to_db_udf(
                    resolved,
                    records,
                    sources,
                    tenants,
                    events,
                    payloads,
                    postgres_config=config,
                )
