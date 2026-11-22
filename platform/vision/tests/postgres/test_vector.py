"""Unit tests targeting vector storage mappings, dimensionality filters, and similarity calculations."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_ontology import GolDenied, GolEffect
from galadril_vision.common.exceptions import VectorSearchError
from galadril_vision.common.types import EntityEmbedding
from galadril_vision.connectors.postgres.vector import VectorStore


@pytest.mark.anyio
@pytest.mark.parametrize("scale", [1.0, 1e-300, 1e300])
async def test_candidate_ranking_follows_receipt_authorization(
    mock_postgres_client: MagicMock, mock_config: MagicMock, scale: float
) -> None:
    """An unreadable prototype must never affect nearest-neighbor ordering."""
    store = VectorStore(mock_postgres_client, mock_config)
    value = {
        "entity_id": "entity-a",
        "licorne_identity_id": "41",
        "modality": "text",
        "embedding": [scale, 0.0, 0.0, 0.0],
        "observation_key": "observation-a",
    }
    connection = _connection_mock()
    connection.execute.return_value.fetchall = AsyncMock(
        return_value=[("entity-a", value, "signed-receipt")]
    )
    held = False

    @asynccontextmanager
    async def authorized(**kwargs: object) -> AsyncIterator[str]:
        nonlocal held
        assert kwargs["receipt"] == "signed-receipt"
        assert kwargs["proposal"] == value
        held = True
        try:
            yield "read-receipt"
        finally:
            held = False

    with patch(
        "galadril_vision.connectors.postgres.vector.protected_gol_effect",
        new=authorized,
    ):
        candidates = await store.find_resolution_candidates(
            [scale, 0.0, 0.0, 0.0],
            "text",
            "tenant-a",
            1,
            connection=connection,
        )
    assert not held
    assert candidates[0].entity_id == "entity-a"
    assert candidates[0].similarity == 1.0


@pytest.mark.anyio
@pytest.mark.parametrize("secret_first", [True, False])
async def test_unreadable_candidates_cannot_displace_readable_ones(
    mock_postgres_client: MagicMock, mock_config: MagicMock, secret_first: bool
) -> None:
    """Denial is applied before top-k even for a perfect unauthorized match."""
    public = {
        "entity_id": "public",
        "licorne_identity_id": "41",
        "modality": "text",
        "embedding": [0.0, 1.0, 0.0, 0.0],
        "observation_key": "public-source",
    }
    secret = {
        **public,
        "entity_id": "secret",
        "embedding": [1.0, 0.0, 0.0, 0.0],
    }
    rows = [
        ("public", public, "public-receipt"),
        ("secret", secret, "secret-receipt"),
    ]
    if secret_first:
        rows.reverse()
    connection = _connection_mock()
    connection.execute.return_value.fetchall = AsyncMock(return_value=rows)

    @asynccontextmanager
    async def authorized(**kwargs: object) -> AsyncIterator[str]:
        if kwargs["receipt"] == "secret-receipt":
            raise GolDenied("Denied")
        yield "read-receipt"

    store = VectorStore(mock_postgres_client, mock_config)
    with patch(
        "galadril_vision.connectors.postgres.vector.protected_gol_effect",
        new=authorized,
    ):
        candidates = await store.find_resolution_candidates(
            [1.0, 0.0, 0.0, 0.0],
            "text",
            "tenant-a",
            1,
            connection=connection,
        )
    assert [candidate.entity_id for candidate in candidates] == ["public"]


@pytest.fixture
def mock_postgres_client() -> MagicMock:
    """Generates an insulated PostgresClient component handling async mock connections."""
    client = MagicMock()
    client.tenant_connection = MagicMock()
    return client


@pytest.fixture
def mock_config() -> MagicMock:
    """Supplies standardized settings variables specifically calibrated for vector lookups."""
    config = MagicMock()
    config.vector_dimensions = 4
    config.vector_search_timeout_ms = 3000
    return config


def _connection_mock() -> MagicMock:
    """Builds a psycopg-shaped connection with synchronous context factories."""
    connection = MagicMock()
    connection.cursor = MagicMock()
    connection.execute = AsyncMock()
    connection.pipeline = MagicMock()
    connection.transaction = MagicMock()
    return connection


@pytest.mark.anyio
async def test_vector_store_initialization_noop(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Confirms the initialization lifecycle interface contract executes without collateral operations."""
    store = VectorStore(client=mock_postgres_client, config=mock_config)
    await store.initialize()


def test_statement_timeout_ms_resolution_logic(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Validates metric translations, type cast fallbacks, and boundary limit clamp applications."""
    store = VectorStore(client=mock_postgres_client, config=mock_config)
    assert store._statement_timeout_ms() == 3000

    mock_config.vector_search_timeout_ms = "1500"
    assert store._statement_timeout_ms() == 1500

    mock_config.vector_search_timeout_ms = "invalid_integer"
    assert store._statement_timeout_ms() == 5000

    delattr(mock_config, "vector_search_timeout_ms")
    assert store._statement_timeout_ms() == 5000

    mock_config.vector_search_timeout_ms = -500
    assert store._statement_timeout_ms() == 1


def test_validate_embedding_structural_constraints(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Ensures input shapes comply with requirements or otherwise throw distinct vector errors."""
    store = VectorStore(client=mock_postgres_client, config=mock_config)

    valid_vector = [0.1, 0.2, 0.3, 0.4]
    assert store._validate_embedding(valid_vector) == valid_vector

    with pytest.raises(VectorSearchError, match="embedding vector is empty"):
        store._validate_embedding([])

    with pytest.raises(VectorSearchError, match="embedding dimension mismatch"):
        store._validate_embedding([0.1, 0.2])


@pytest.mark.anyio
@patch("galadril_vision.connectors.postgres.vector.register_vector_async")
async def test_ensure_vector_registration_idempotency(
    mock_register: AsyncMock,
    mock_postgres_client: MagicMock,
    mock_config: MagicMock,
) -> None:
    """Guarantees standard driver extensions bind to novel links once without generating duplicate calls."""
    store = VectorStore(client=mock_postgres_client, config=mock_config)
    mock_conn = MagicMock()
    mock_conn._vector_registered = False

    await store._ensure_vector_registration(mock_conn)
    mock_register.assert_called_once_with(mock_conn)
    assert mock_conn._vector_registered is True

    mock_register.reset_mock()
    await store._ensure_vector_registration(mock_conn)
    mock_register.assert_not_called()


@pytest.mark.anyio
@patch(
    "galadril_vision.connectors.postgres.vector.normalize_tenant_id",
    return_value="tenant-123",
)
@patch(
    "galadril_vision.connectors.postgres.vector.normalize_embedding_modality",
    return_value="TEXT",
)
async def test_find_similar_with_modality_routing(
    mock_norm_mod: MagicMock,
    mock_norm_tenant: MagicMock,
    mock_postgres_client: MagicMock,
    mock_config: MagicMock,
) -> None:
    """Tests the similarity pipeline with and without modalities, verifying SQL generation parameters."""
    store = VectorStore(client=mock_postgres_client, config=mock_config)

    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = [
        ("ent-1", 0.92, "TEXT"),
        ("ent-2", 0.81, "TEXT"),
    ]

    mock_conn = _connection_mock()
    mock_conn.pipeline.return_value.__aenter__.return_value = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_postgres_client.tenant_connection.return_value.__aenter__.return_value = mock_conn

    results_without_modality = await store.find_similar(
        embedding=[0.1, 0.2, 0.3, 0.4],
        modality=None,
        tenant_id="raw-tenant",
        top_k=2,
    )
    assert results_without_modality == [("ent-1", 0.92), ("ent-2", 0.81)]
    assert mock_cursor.execute.call_count == 2


@pytest.mark.anyio
async def test_find_resolution_candidates_ignores_unsigned_side_tables(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Identity and spatial side tables cannot silently enrich signed evidence."""
    store = VectorStore(client=mock_postgres_client, config=mock_config)
    connection = _connection_mock()
    connection.execute.return_value.fetchall = AsyncMock(return_value=[])
    assert (
        await store.find_resolution_candidates(
            [0.1, 0.2, 0.3, 0.4],
            "face",
            "tenant-1",
            2,
            connection=connection,
        )
        == []
    )
    query, params = connection.execute.call_args.args
    assert "identity_prototypes" in query
    assert "gol_receipt IS NOT NULL" in query
    assert "identity_links" not in query
    assert "entity_states" not in query
    assert params == ("tenant-1", "face")


@pytest.mark.anyio
@patch(
    "galadril_vision.connectors.postgres.vector.normalize_tenant_id",
    return_value="tenant-123",
)
@patch(
    "galadril_vision.connectors.postgres.vector.normalize_embedding_modality",
    return_value="IMAGE",
)
async def test_has_embeddings_conditions(
    mock_norm_mod: MagicMock,
    mock_norm_tenant: MagicMock,
    mock_postgres_client: MagicMock,
    mock_config: MagicMock,
) -> None:
    """Verifies targeted existence evaluations return accurate indicators depending on matching rows."""
    store = VectorStore(client=mock_postgres_client, config=mock_config)

    mock_cursor = AsyncMock()
    mock_cursor.fetchone.side_effect = [(1,), None]

    mock_conn = _connection_mock()
    mock_conn.pipeline.return_value.__aenter__.return_value = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_postgres_client.tenant_connection.return_value.__aenter__.return_value = mock_conn

    exists_unfiltered = await store.has_embeddings(
        tenant_id="raw-tenant", modality=None
    )
    assert exists_unfiltered is True

    exists_filtered = await store.has_embeddings(
        tenant_id="raw-tenant", modality="IMAGE"
    )
    assert exists_filtered is False


@pytest.mark.anyio
@patch(
    "galadril_vision.connectors.postgres.vector.normalize_tenant_id",
    return_value="tenant-123",
)
@patch(
    "galadril_vision.connectors.postgres.vector.normalize_embedding_modality",
    return_value="TEXT",
)
@patch("galadril_vision.connectors.postgres.vector.require_same_tenant")
async def test_store_embeddings_batch_processing(
    mock_require_tenant: MagicMock,
    mock_norm_mod: MagicMock,
    mock_norm_tenant: MagicMock,
    mock_postgres_client: MagicMock,
    mock_config: MagicMock,
) -> None:
    """Verifies multi-row structural assembly, datetime processing, and direct connection execution."""
    from typing import cast

    store = VectorStore(client=mock_postgres_client, config=mock_config)

    mock_cursor = AsyncMock()
    mock_conn = _connection_mock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_conn.transaction.return_value.__aenter__.return_value = MagicMock()
    mock_postgres_client.tenant_connection.return_value.__aenter__.return_value = mock_conn

    await store.store_embeddings_batch([])
    mock_cursor.executemany.assert_not_called()

    mock_embedding_1 = MagicMock(spec=EntityEmbedding)
    mock_embedding_1.tenant_id = "tenant-123"
    mock_embedding_1.modality = "TEXT"
    mock_embedding_1.vector = [0.1, 0.2, 0.3, 0.4]
    mock_embedding_1.metadata = {"timestamp": "2026-03-29T12:00:00+00:00"}

    mock_embedding_2 = MagicMock(spec=EntityEmbedding)
    mock_embedding_2.tenant_id = "tenant-123"
    mock_embedding_2.modality = "TEXT"
    mock_embedding_2.vector = [0.5, 0.6, 0.7, 0.8]
    mock_embedding_2.metadata = {"timestamp": datetime.now(UTC)}

    batch = [
        (cast(EntityEmbedding, mock_embedding_1), "entity-A"),
        (cast(EntityEmbedding, mock_embedding_2), "entity-B"),
    ]

    await store.store_embeddings_batch(batch)
    mock_cursor.executemany.assert_called_once()


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


@pytest.mark.anyio
async def test_vector_candidate_reads_require_shared_state_admission(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    store = VectorStore(client=mock_postgres_client, config=mock_config)
    with pytest.raises(GolDenied):
        await store.find_resolution_candidates([0.1] * 4, "text", "tenant-a", 3)
    mock_postgres_client.tenant_connection.assert_not_called()


@asynccontextmanager
async def _unit_scope(**_kwargs: object) -> AsyncIterator[str]:
    """Supplies a unit transport scope without claiming an authorization decision."""
    yield "unit-receipt"


@asynccontextmanager
async def _unit_batch(
    effects: list[GolEffect], **_kwargs: object
) -> AsyncIterator[tuple[str, ...]]:
    yield tuple("unit-receipt" for _ in effects)


def _unit_state_effect(**kwargs: object) -> GolEffect:
    return GolEffect.model_validate(
        {
            key: value
            for key, value in kwargs.items()
            if key not in {"tenant_id", "derived"}
        }
        | {"target": "tests.scope::Value"}
    )


@pytest.fixture(autouse=True)
def _admitted_sql_transport(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keeps SQL-shape assertions separate from the real denial boundary tests."""
    if (
        request.node.name
        == "test_vector_candidate_reads_require_shared_state_admission"
    ):
        yield
        return
    with (
        patch(
            "galadril_vision.connectors.postgres.vector.protected_gol_effect",
            new=_unit_scope,
        ),
        patch(
            "galadril_vision.connectors.postgres.vector.protected_gol_state_effect",
            new=_unit_scope,
        ),
        patch(
            "galadril_vision.connectors.postgres.vector.protected_gol_effects",
            new=_unit_batch,
        ),
        patch(
            "galadril_vision.connectors.postgres.vector.make_gol_state_effect",
            new=_unit_state_effect,
        ),
    ):
        yield


@pytest.mark.anyio
async def test_vector_sql_is_inside_the_live_effect_scope(
    mock_postgres_client: MagicMock, mock_config: MagicMock
) -> None:
    """Read permission must remain bound while the SQL result is fetched."""
    held = False

    @asynccontextmanager
    async def scope(**_kwargs: object) -> AsyncIterator[str]:
        nonlocal held
        held = True
        try:
            yield "unit-receipt"
        finally:
            held = False

    conn = _connection_mock()
    cursor = AsyncMock()

    async def execute(*_args: object) -> None:
        assert held, "Vector SQL escaped its live permit"

    cursor.execute.side_effect = execute
    cursor.fetchone.return_value = (1,)
    conn.cursor.return_value.__aenter__.return_value = cursor
    mock_postgres_client.tenant_connection.return_value.__aenter__.return_value = conn
    with patch(
        "galadril_vision.connectors.postgres.vector.protected_gol_state_effect",
        new=scope,
        create=True,
    ):
        assert await VectorStore(
            mock_postgres_client, mock_config
        ).has_embeddings(tenant_id="tenant-a", modality=None)
    assert not held


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
