"""Verifies GOL source authoring and secure published projections in Chromium."""

import pytest
from assertions import eventually
from authentication import (
    browser_cookie_snapshot,
    initialize_authentication,
    login_user,
)
from browser import run_browser_suite
from clients import (
    OUTSIDER_ID,
    POSTGRES_DSN,
    TENANT_ID,
    UPLOADER_ID,
    GatewayClient,
    RelationshipSpec,
    SpiceDBProbe,
    gateway_tls_context,
    seed_users,
)
from environment import pipeline_environment

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Exercises the same asyncio driver as the running services."""
    return "asyncio"


async def test_gol_browser_lifecycle() -> None:
    """Runs the editor and runtime through the real Gateway and Registry."""
    gateway = GatewayClient()
    spicedb = SpiceDBProbe()
    try:
        async with pipeline_environment() as environment:
            await eventually(
                environment.authentication_ready,
                timeout_seconds=90,
                description="GOL browser identity readiness",
            )
            await seed_users()
            initialize_authentication(
                environment.identity_request,
                "https://127.0.0.1:18080",
                POSTGRES_DSN,
                gateway_tls_context(),
            )
            token = await login_user(UPLOADER_ID)
            await login_user(OUTSIDER_ID)
            await spicedb.touch(
                RelationshipSpec(
                    "tenant", TENANT_ID, "member", "user", UPLOADER_ID
                ),
                RelationshipSpec(
                    "tenant", TENANT_ID, "administrator", "user", UPLOADER_ID
                ),
                RelationshipSpec(
                    "tenant", TENANT_ID, "member", "user", OUTSIDER_ID
                ),
            )

            # Exact metadata grants do not grant any instance data or mailbox ownership.
            await spicedb.touch(
                *(
                    RelationshipSpec(
                        "gol_resource",
                        "-".join(
                            part.encode("utf-8").hex()
                            for part in (
                                TENANT_ID,
                                "workspace.browser",
                                name,
                                "__schema",
                            )
                        ),
                        "metadata_readers",
                        "user",
                        OUTSIDER_ID,
                    )
                    for name in (
                        "BrowserMessage",
                        "BrowserAttachment",
                        "BrowserAttached",
                    )
                )
            )

            async def ready() -> bool:
                data = await gateway.execute(
                    token, "query{golOntologies{ontologyId}}"
                )
                return isinstance(data.get("golOntologies"), list)

            await eventually(
                ready, timeout_seconds=90, description="GOL Gateway readiness"
            )
            await run_browser_suite(
                "gol-live.spec.ts",
                {
                    "cookies": browser_cookie_snapshot(UPLOADER_ID, TENANT_ID),
                    "memberCookies": browser_cookie_snapshot(
                        OUTSIDER_ID, TENANT_ID
                    ),
                    "memberId": OUTSIDER_ID,
                },
            )
    finally:
        await gateway.close()


if __name__ == "__main__":
    raise SystemExit(
        pytest.main([__file__, "--import-mode=importlib", "--capture=no"])
    )
