"""Chatbot and Pengolo over one fresh, live causal cohort."""

from __future__ import annotations

import sys

import pytest
from chatbot import exercise_chatbot
from gateway_scenario import (
    gateway_scenario,
    provision_fixture_administrator,
    revoke_fixture_admin,
)
from pengolo import exercise_pengolo
from pipeline_causal import exercise_causal_branch
from pipeline_ingestion import ingest_record
from pipeline_publication import start_streaming_pipeline
from scenario import ADMIN

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Runs the lifecycle on the production asyncio backend only."""
    return "asyncio"


async def test_pipeline_assistant() -> None:
    """Checks durable replay, scoped tool evidence, revocation and Chromium flows."""
    async with gateway_scenario(
        tenant_aliases=("other_tenant",), models=True
    ) as scenario:
        await start_streaming_pipeline(scenario)
        state, _, _ = await ingest_record(scenario)
        await exercise_causal_branch(
            scenario.gateway, scenario.spicedb, scenario.uploader_token
        )
        await provision_fixture_administrator(scenario, ADMIN)
        await revoke_fixture_admin(scenario)
        await exercise_chatbot(scenario.gateway, scenario.spicedb, state)
        await exercise_pengolo(scenario.gateway, state, administrator=ADMIN)


if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [__file__, "--import-mode=importlib", "--capture=no", *sys.argv[1:]]
        )
    )
