"""Live authentication and authorization at the Gateway boundary."""

from __future__ import annotations

import sys

import pytest
from gateway_scenario import gateway_scenario, provision_fixture_administrator
from pipeline_authorization import (
    exercise_gateway_authorization,
    exercise_revoked_administrator,
)
from scenario import ADMIN

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Runs the lifecycle on the production asyncio backend only."""
    return "asyncio"


async def test_gateway_authorization() -> None:
    """Checks the GraphQL surface and independently revokes the fixture admin."""
    async with gateway_scenario(
        tenant_aliases=("isolated_e2e_tenant",)
    ) as scenario:
        await exercise_gateway_authorization(scenario)
        await provision_fixture_administrator(scenario, ADMIN)
        await exercise_revoked_administrator(scenario, ADMIN)


if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [__file__, "--import-mode=importlib", "--capture=no", *sys.argv[1:]]
        )
    )
