"""Complete lifecycle benchmark against one isolated real-service stack."""

from __future__ import annotations

import asyncio
import sys

import pytest
from chatbot import exercise_chatbot
from environment import (
    PIPELINE_LIFECYCLE_TIMEOUT_SECONDS,
)
from gateway_scenario import gateway_scenario
from pengolo import exercise_pengolo
from pipeline_authoring import exercise_pipeline_authoring
from pipeline_authorization import (
    exercise_gateway_authorization,
    exercise_revoked_administrator,
)
from pipeline_causal import exercise_causal_branch
from pipeline_empirical import run_empirical_scenarios
from pipeline_ingestion import (
    assert_ingestion_access,
    assert_ingestion_lineage,
    ingest_record,
)
from pipeline_publication import retire_pipeline, start_streaming_pipeline
from pipeline_search import exercise_query_api
from scenario import ADMIN

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Runs the lifecycle on the production asyncio backend only."""
    return "asyncio"


async def _run_complete_lifecycle() -> None:
    async with gateway_scenario(
        tenant_aliases=("isolated_e2e_tenant", "other_tenant"), models=True
    ) as scenario:
        await exercise_gateway_authorization(scenario)
        head = await start_streaming_pipeline(
            scenario, review=exercise_pipeline_authoring
        )
        state, object_key, source_raw_id = await ingest_record(
            scenario, check_upload_boundaries=True
        )
        await exercise_query_api(
            scenario.gateway, scenario.uploader_token, state.entity_id
        )
        await exercise_causal_branch(
            scenario.gateway, scenario.spicedb, scenario.uploader_token
        )
        await run_empirical_scenarios(scenario, full=True)
        await exercise_revoked_administrator(scenario, ADMIN)
        await assert_ingestion_access(
            scenario, state, object_key, source_raw_id
        )
        await assert_ingestion_lineage(state)
        await exercise_chatbot(scenario.gateway, scenario.spicedb, state)
        await exercise_pengolo(scenario.gateway, state, administrator=ADMIN)
        await retire_pipeline(scenario, head)


async def test_complete_pipeline_lifecycle() -> None:
    """Exercises all cohorts with an internal deadline that leaves teardown time."""
    try:
        await asyncio.wait_for(
            _run_complete_lifecycle(),
            timeout=PIPELINE_LIFECYCLE_TIMEOUT_SECONDS,
        )
    except TimeoutError as error:
        raise AssertionError(
            "The complete lifecycle exceeded its "
            f"{PIPELINE_LIFECYCLE_TIMEOUT_SECONDS:.0f}-second internal deadline"
        ) from error


if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [__file__, "--import-mode=importlib", "--capture=no", *sys.argv[1:]]
        )
    )
