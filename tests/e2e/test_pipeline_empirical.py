"""Real-source identity resolution, retrieval and independent causal oracles."""

from __future__ import annotations

import sys

import pytest
from gateway_scenario import gateway_scenario
from pipeline_empirical import run_empirical_scenarios
from pipeline_ingestion import ingest_record
from pipeline_publication import start_streaming_pipeline
from pipeline_search import exercise_query_api

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Runs the lifecycle on the production asyncio backend only."""
    return "asyncio"


async def test_empirical_pipeline() -> None:
    """Builds its own retail identities, search corpus and randomized-trial evidence."""
    async with gateway_scenario(models=True) as scenario:
        await start_streaming_pipeline(scenario)
        state, _, _ = await ingest_record(scenario)
        await exercise_query_api(
            scenario.gateway, scenario.uploader_token, state.entity_id
        )
        await run_empirical_scenarios(scenario, full=False)


if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [__file__, "--import-mode=importlib", "--capture=no", *sys.argv[1:]]
        )
    )
