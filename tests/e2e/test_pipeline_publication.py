"""Real Registry publication and retirement without streaming workers."""

from __future__ import annotations

import sys

import pytest
from gateway_scenario import gateway_scenario
from pipeline_authoring import exercise_pipeline_authoring
from pipeline_publication import provision_pipeline, retire_pipeline

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Runs the lifecycle on the production asyncio backend only."""
    return "asyncio"


async def test_pipeline_publication() -> None:
    """Checks review, unauthorized publication, runtime bindings and retirement."""
    async with gateway_scenario() as scenario:
        head = await provision_pipeline(
            scenario, review=exercise_pipeline_authoring
        )
        await retire_pipeline(scenario, head)


if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [__file__, "--import-mode=importlib", "--capture=no", *sys.argv[1:]]
        )
    )
