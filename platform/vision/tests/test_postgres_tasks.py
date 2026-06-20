"""Unit tests for async Postgres batch helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from galadril_vision.pipeline import postgres_tasks


def test_resolve_entities_batch_marks_unknown_when_index_is_empty() -> None:
    """Verify resolution short-circuits when no embeddings exist for a tenant."""
    vector_store = AsyncMock()
    vector_store.has_embeddings.return_value = False
    vector_store.find_similar.return_value = [("person-1", 0.99)]

    async def _stores(_: object, __: object) -> tuple[Any, Any, Any]:
        return object(), vector_store, object()

    inference_results = [
        {
            "prediction": {
                "faces": [
                    {
                        "embedding": [0.1, 0.2, 0.3, 0.4],
                        "confidence": 0.93,
                    }
                ]
            },
            "confidence": 0.93,
            "model_name": "face_recognition",
            "model_version": "1.0.0",
            "error": None,
        }
    ]
    postgres_config = type(
        "Config",
        (),
        {"vector_dimensions": 4, "vector_search_timeout_ms": 100},
    )()
    state = postgres_tasks.PostgresRuntimeState()

    with patch.object(postgres_tasks, "get_pg_stores", side_effect=_stores):
        result = asyncio.run(
            postgres_tasks.resolve_entities_batch(
                state=state,
                postgres_config=postgres_config,
                inference_results=inference_results,
                tenant_ids=["tenant-a"],
                modality="face_recognition",
                threshold=0.7,
            )
        )

    assert len(result) == 1
    assert len(result[0]) == 1
    assert result[0][0]["is_unknown"] is True
    assert result[0][0]["resolved_entity_id"].startswith(
        "unknown_face_recognition_"
    )
    vector_store.has_embeddings.assert_awaited_once()
    vector_store.find_similar.assert_not_called()
