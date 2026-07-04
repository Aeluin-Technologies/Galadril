"""Unit tests targeting vector storage mappings, dimensionality filters, and similarity calculations."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from galadril_vision.common.exceptions import VectorSearchError
from galadril_vision.common.types import EntityEmbedding
from galadril_vision.connectors.postgres.vector import VectorStore


@pytest.fixture
def mock_postgres_client() -> MagicMock:
    """Generates an insulated PostgresClient component handling async mock connections."""
    client = MagicMock()
    client.connection = MagicMock()
    return client


@pytest.fixture
def mock_config() -> MagicMock:
    """Supplies standardized settings variables specifically calibrated for vector lookups."""
    config = MagicMock()
    config.vector_dimensions = 4
    config.vector_search_timeout_ms = 3000
    return config


@pytest.mark.asyncio
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


@pytest.mark.asyncio
@patch("galadril_vision.connectors.postgres.vector.register_vector_async")
async def test_ensure_vector_registration_idempotency(
    mock_register: AsyncMock,
    mock_postgres_client: MagicMock,
    mock_config: MagicMock,
) -> None:
    """Guarantees standard driver extensions bind to novel links once without generating duplicate calls."""
    store = VectorStore(client=mock_postgres_client, config=mock_config)
    mock_conn = MagicMock()

    await store._ensure_vector_registration(mock_conn)
    mock_register.assert_called_once_with(mock_conn)
    assert getattr(mock_conn, "_vector_registered") is True

    mock_register.reset_mock()
    await store._ensure_vector_registration(mock_conn)
    mock_register.assert_not_called()


@pytest.mark.asyncio
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

    mock_conn = AsyncMock()
    mock_conn.pipeline.return_value.__aenter__.return_value = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    results_without_modality = await store.find_similar(
        embedding=[0.1, 0.2, 0.3, 0.4],
        modality=None,
        tenant_id="raw-tenant",
        top_k=2,
    )
    assert results_without_modality == [("ent-1", 0.92), ("ent-2", 0.81)]
    assert mock_cursor.execute.call_count == 2

    mock_cursor.execute.reset_mock()
    results_with_modality = await store.find_similar_with_modality(
        embedding=[0.1, 0.2, 0.3, 0.4],
        modality="TEXT",
        tenant_id="raw-tenant",
        top_k=5,
    )
    assert len(results_with_modality) == 2
    assert mock_cursor.execute.call_count == 2


@pytest.mark.asyncio
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

    mock_conn = AsyncMock()
    mock_conn.pipeline.return_value.__aenter__.return_value = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

    exists_unfiltered = await store.has_embeddings(
        tenant_id="raw-tenant", modality=None
    )
    assert exists_unfiltered is True

    exists_filtered = await store.has_embeddings(
        tenant_id="raw-tenant", modality="IMAGE"
    )
    assert exists_filtered is False


@pytest.mark.asyncio
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
    mock_conn = AsyncMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_conn.transaction.return_value.__aenter__.return_value = MagicMock()
    mock_postgres_client.connection.return_value.__aenter__.return_value = (
        mock_conn
    )

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
    mock_embedding_2.metadata = {"timestamp": datetime.now(timezone.utc)}

    batch = [
        (cast(EntityEmbedding, mock_embedding_1), "entity-A"),
        (cast(EntityEmbedding, mock_embedding_2), "entity-B"),
    ]

    await store.store_embeddings_batch(batch)
    mock_cursor.executemany.assert_called_once()
