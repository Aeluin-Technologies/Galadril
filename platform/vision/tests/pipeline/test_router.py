"""Unit tests targeting the multi-tenant LRU caching structures and asynchronous routing layers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_pipeline.runtime.batch import PipelineResult
from galadril_vision.pipeline.router import (
    MultiTenantPipelineRouter,
    PipelineRouteKey,
    TrackedExecutor,
)


def test_pipeline_route_key_equality_and_hash() -> None:
    """Validates structural equivalence and hash collisions for composite key lookups."""
    key_a1 = PipelineRouteKey(tenant_id="t1", topic="video")
    key_a2 = PipelineRouteKey(tenant_id="t1", topic="video")
    key_b = PipelineRouteKey(tenant_id="t2", topic="video")

    assert key_a1 == key_a2
    assert key_a1 != key_b
    assert hash(key_a1) == hash(key_a2)
    assert hash(key_a1) != hash(key_b)


@pytest.mark.asyncio
async def test_tracked_executor_lifecycle_and_drain() -> None:
    """Verifies active transaction accounting and safe connection pool drainage."""
    mock_base_executor = AsyncMock()
    mock_base_executor.execute.return_value = PipelineResult(
        processed_records=5, duration=1.2
    )
    mock_base_executor._pg_client = AsyncMock()

    tracked = TrackedExecutor(mock_base_executor)
    assert tracked.active_count == 0

    res = await tracked.execute_parquet("s3://parquet")
    assert res.processed_records == 5
    assert tracked.active_count == 0

    await tracked.safe_close()
    mock_base_executor._pg_client.close.assert_called_once()

    with pytest.raises(
        RuntimeError, match="Cannot execute graph on a closing executor pool"
    ):
        await tracked.execute_parquet("s3://parquet")


@pytest.mark.asyncio
@patch("galadril_vision.pipeline.router.S3Client")
async def test_router_configuration_fetching_and_matching(
    mock_s3_cls: MagicMock,
) -> None:
    """Validates configuration extraction loops and route assignment hooks via S3 mappings."""
    mock_s3 = AsyncMock()
    mock_s3.list_object_keys.return_value = ["t1/pipelines/video.yaml"]
    mock_s3.get_object_bytes.return_value = b"sources:\n  - id: s1\n    topic: video\npostgres:\n  dsn: postgres://x"
    mock_s3_cls.return_value = mock_s3

    router = MultiTenantPipelineRouter(config_bucket="configs")

    with patch.object(
        router, "_discover_and_build_executor", new_callable=AsyncMock
    ) as mock_build:
        mock_exec = AsyncMock()
        mock_build.return_value = mock_exec

        rk = PipelineRouteKey("t1", "video")
        await router.pre_warm_tenant_pipeline("t1", "video")

        assert router._cache.get(rk) is mock_exec
