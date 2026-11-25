"""Transversal Gateway upload through the real distributed pipeline."""

from __future__ import annotations

import sys

import pytest
from gateway_scenario import gateway_scenario
from pipeline_ingestion import (
    assert_ingestion_access,
    assert_ingestion_lineage,
    ingest_record,
)
from pipeline_publication import start_streaming_pipeline

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Runs the lifecycle on the production asyncio backend only."""
    return "asyncio"


async def test_gateway_upload_reaches_authorized_gateway_access() -> None:
    """Proves persistence, derived permissions and trace propagation on one record."""
    async with gateway_scenario(
        tenant_aliases=("isolated_e2e_tenant",)
    ) as scenario:
        await start_streaming_pipeline(scenario)
        state, object_key, source_raw_id = await ingest_record(
            scenario, check_upload_boundaries=True
        )
        await assert_ingestion_access(
            scenario, state, object_key, source_raw_id
        )
        await assert_ingestion_lineage(state)


if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [__file__, "--import-mode=importlib", "--capture=no", *sys.argv[1:]]
        )
    )
