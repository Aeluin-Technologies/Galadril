"""Unit tests targeting transactional reliable outbox dispatch loops and backoff mechanisms."""

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import orjson
import pytest
from galadril_vision.common.config import (
    KafkaConnectorConfig,
    SpiceDBConnectorConfig,
)
from galadril_vision.common.exceptions import TenantIsolationError
from galadril_vision.connectors.authz.outbox import (
    AuthzOutboxFlusher,
    OutboxRow,
    _compute_backoff,
)
from galadril_vision.connectors.authz.spicedb import SpiceDBWriter
from galadril_vision.connectors.kafka.producer import KafkaJsonProducer

MockDependencies = tuple[
    SpiceDBConnectorConfig,
    KafkaConnectorConfig,
    AsyncMock,
    MagicMock,
]


def _mock_connection() -> AsyncMock:
    """Builds psycopg's synchronous transaction factory around async hooks."""
    connection = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    connection.transaction = MagicMock(return_value=transaction)
    return connection


@pytest.fixture
def mock_dependencies() -> tuple[
    SpiceDBConnectorConfig,
    KafkaConnectorConfig,
    AsyncMock,
    MagicMock,
]:
    """Assembles all mocked configurations and IO interfaces required by the flusher loop."""
    spicedb_cfg = SpiceDBConnectorConfig(
        endpoint="localhost:50051",
        token="test",
        max_local_retries=2,
        base_retry_ms=50,
        max_retry_ms=200,
    )
    kafka_cfg = KafkaConnectorConfig(
        brokers=["localhost:9092"],
        schema_registry="http://localhost:8081",
        consumer_group="test",
    )
    dlq_producer = AsyncMock(spec=KafkaJsonProducer)
    writer = MagicMock(spec=SpiceDBWriter)

    return spicedb_cfg, kafka_cfg, dlq_producer, writer


def test_exponential_backoff_calculations() -> None:
    """Confirms calculation variations stay bound inside predefined limits."""
    for attempt in range(1, 5):
        delay = _compute_backoff(base_ms=10, max_ms=100, attempt=attempt)
        assert 0 <= delay <= 100


def test_split_reference_fallbacks(mock_dependencies: MockDependencies) -> None:
    """Validates string decomposition rules and warning overrides for flat text entries."""
    spicedb_cfg, kafka_cfg, dlq_producer, writer = mock_dependencies
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=spicedb_cfg,
        kafka_cfg=kafka_cfg,
        dlq_producer=dlq_producer,
        writer=writer,
        subject_normalization_type="group",
    )

    t_type, t_id = flusher._split_reference("admin_team", "subject")
    assert t_type == "group"
    assert t_id == "admin_team"

    with pytest.raises(
        TenantIsolationError, match="subject reference is malformed"
    ):
        flusher_no_fallback = AuthzOutboxFlusher(
            spicedb_cfg=spicedb_cfg,
            kafka_cfg=kafka_cfg,
            dlq_producer=dlq_producer,
            writer=writer,
        )
        flusher_no_fallback._split_reference("untyped_value", "subject")

    with pytest.raises(
        TenantIsolationError, match="resource reference is incomplete"
    ):
        flusher._split_reference("resource:", "resource")


def test_scope_resource_routing_rules(
    mock_dependencies: MockDependencies,
) -> None:
    """Verifies prefix generation mappings for multi-tenant containment contexts."""
    spicedb_cfg, kafka_cfg, dlq_producer, writer = mock_dependencies
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=spicedb_cfg,
        kafka_cfg=kafka_cfg,
        dlq_producer=dlq_producer,
        writer=writer,
    )

    assert flusher._scope_resource("t1", "file:t1/doc.pdf") == "file:t1/doc.pdf"
    with pytest.raises(
        TenantIsolationError, match="not explicitly tenant scoped"
    ):
        flusher._scope_resource("t1", "file:plain_id")

    with pytest.raises(
        TenantIsolationError, match="not explicitly tenant scoped"
    ):
        flusher._scope_resource("t1", "file:t2/stolen_doc")


def test_parse_tuples_data_types(mock_dependencies: MockDependencies) -> None:
    """Asserts accurate transformation maps regardless of string, byte, or raw payload encodings."""
    spicedb_cfg, kafka_cfg, dlq_producer, writer = mock_dependencies
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=spicedb_cfg,
        kafka_cfg=kafka_cfg,
        dlq_producer=dlq_producer,
        writer=writer,
    )

    raw_list = [
        {
            "object": "project:t1/p1",
            "permission": "read",
            "principal": "user:bob",
        }
    ]

    res_bytes = flusher._parse_tuples(
        tuples_json=orjson.dumps(raw_list), tenant_id="t1"
    )
    assert len(res_bytes) == 1
    assert res_bytes[0].relation == "read"

    res_str = flusher._parse_tuples(
        tuples_json=orjson.dumps(raw_list).decode(), tenant_id="t1"
    )
    assert len(res_str) == 1

    with pytest.raises(TenantIsolationError, match="tuples_json is missing"):
        flusher._parse_tuples(tuples_json=None, tenant_id="t1")

    with pytest.raises(
        TenantIsolationError, match="tuples_json must be a list"
    ):
        flusher._parse_tuples(tuples_json='"not_a_list"', tenant_id="t1")


@pytest.mark.anyio
async def test_claim_due_rows_success_and_poison_pill(
    mock_dependencies: MockDependencies,
) -> None:
    """Verifies row lease state machine cycles and poison-pill diversion routing for bad records."""
    spicedb_cfg, kafka_cfg, dlq_producer, writer = mock_dependencies
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=spicedb_cfg,
        kafka_cfg=kafka_cfg,
        dlq_producer=dlq_producer,
        writer=writer,
    )

    mock_res = MagicMock()
    mock_res.fetchall = AsyncMock(
        return_value=[
            (
                10,
                "t1",
                "obj_1",
                b'[{"object":"doc:t1/1","relation":"owner","user":"user:a"}]',
                0,
            ),
            (20, "t1", "obj_2", b"invalid_json_bytes", 1),
        ]
    )

    mock_conn = _mock_connection()
    mock_conn.execute.return_value = mock_res

    rows = await flusher._claim_due_rows(conn=mock_conn, limit=5)

    assert len(rows) == 1
    assert rows[0].id == 10
    dlq_producer.produce_json.assert_called_once_with(
        topic=ANY, key="t1:obj_2", payload=ANY
    )
    mock_conn.execute.assert_any_call(ANY, (20, "t1"))


@pytest.mark.anyio
async def test_flush_one_success_path(
    mock_dependencies: MockDependencies,
) -> None:
    """Confirms database row deletions are triggered immediately upon successful transmissions."""
    spicedb_cfg, kafka_cfg, dlq_producer, writer = mock_dependencies
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=spicedb_cfg,
        kafka_cfg=kafka_cfg,
        dlq_producer=dlq_producer,
        writer=writer,
    )

    mock_conn = _mock_connection()

    row = OutboxRow(
        id=1, tenant_id="t1", object_id="obj", tuples=[], attempts=0
    )
    await flusher._flush_one(conn=mock_conn, row=row)

    writer.write_relationships.assert_called_once_with("t1", [])
    mock_conn.execute.assert_called_once_with(
        "DELETE FROM authz_outbox WHERE id = %s AND tenant_id = %s", (1, "t1")
    )


@pytest.mark.anyio
async def test_flush_one_retry_and_dlq_fallback(
    mock_dependencies: MockDependencies,
) -> None:
    """Validates retry tracking step escalations leading up to a structural DLQ eviction."""
    spicedb_cfg, kafka_cfg, dlq_producer, writer = mock_dependencies
    writer.write_relationships.side_effect = Exception("SpiceDB Unavailable")

    flusher = AuthzOutboxFlusher(
        spicedb_cfg=spicedb_cfg,
        kafka_cfg=kafka_cfg,
        dlq_producer=dlq_producer,
        writer=writer,
    )

    mock_conn = _mock_connection()

    low_attempt_row = OutboxRow(
        id=10, tenant_id="t1", object_id="o", tuples=[], attempts=0
    )
    await flusher._flush_one(conn=mock_conn, row=low_attempt_row)
    mock_conn.execute.assert_called_with(ANY, (1, ANY, 10, "t1"))

    mock_conn.execute.reset_mock()
    max_attempt_row = OutboxRow(
        id=20, tenant_id="t1", object_id="o", tuples=[], attempts=2
    )
    await flusher._flush_one(conn=mock_conn, row=max_attempt_row)

    dlq_producer.produce_json.assert_called_once()
    mock_conn.execute.assert_called_with(ANY, (3, ANY, 20, "t1"))


@pytest.mark.anyio
async def test_run_forever_loop_execution(
    mock_dependencies: MockDependencies,
) -> None:
    """Validates graceful termination behavior of continuous execution environments via stop flags."""
    spicedb_cfg, kafka_cfg, dlq_producer, writer = mock_dependencies
    flusher = AuthzOutboxFlusher(
        spicedb_cfg=spicedb_cfg,
        kafka_cfg=kafka_cfg,
        dlq_producer=dlq_producer,
        writer=writer,
    )

    mock_conn = AsyncMock()
    stop_event = asyncio.Event()
    stop_event.set()

    with patch.object(
        flusher, "_claim_due_rows", return_value=[]
    ) as mock_claim:
        await flusher.run_forever(conn=mock_conn, stop_event=stop_event)
        mock_claim.assert_not_called()


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
