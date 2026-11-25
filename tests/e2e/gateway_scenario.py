"""Isolated Gateway sessions for real-service E2E scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from assertions import (
    eventually,
    require_mapping,
)
from authentication import initialize_authentication, login_user
from clients import (
    ONTOLOGY_ID,
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
from environment import (
    ComposeEnvironment,
    pipeline_environment,
)


def _field(data: Mapping[str, object], name: str) -> dict[str, object]:
    """Returns one required GraphQL object field."""
    return require_mapping(data.get(name), f"GraphQL {name}")


def _string(data: Mapping[str, object], name: str) -> str:
    """Returns one required string field with a precise failure."""
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"{name} must be a non-empty string")
    return value


async def _authorize_fixture_principals(spicedb: SpiceDBProbe) -> None:
    """Seeds only the identities and ownership absent from public APIs."""
    await spicedb.touch(
        RelationshipSpec("tenant", TENANT_ID, "member", "user", UPLOADER_ID),
        RelationshipSpec(
            "tenant", TENANT_ID, "administrator", "user", UPLOADER_ID
        ),
        RelationshipSpec("tenant", TENANT_ID, "member", "user", OUTSIDER_ID),
        RelationshipSpec(
            "ontology",
            f"{TENANT_ID}/{ONTOLOGY_ID}",
            "parent",
            "tenant",
            TENANT_ID,
        ),
        RelationshipSpec(
            "ontology",
            f"{TENANT_ID}/{ONTOLOGY_ID}",
            "owner",
            "user",
            UPLOADER_ID,
        ),
    )


@dataclass(frozen=True, slots=True)
class GatewayScenario:
    """Holds the clients and principals backed by one Compose project."""

    environment: ComposeEnvironment
    gateway: GatewayClient
    spicedb: SpiceDBProbe
    uploader_token: str
    outsider_token: str


@asynccontextmanager
async def gateway_scenario(
    *, tenant_aliases: tuple[str, ...] = (), models: bool = False
) -> AsyncIterator[GatewayScenario]:
    """Issues real identity tokens and installs only the fixture bootstrap grants."""
    async with pipeline_environment() as environment:
        gateway = GatewayClient()
        spicedb = SpiceDBProbe()
        try:
            await eventually(
                environment.authentication_ready,
                timeout_seconds=90.0,
                description="identity migrations and public Studio readiness",
            )
            await seed_users()
            initialize_authentication(
                environment.identity_request,
                "https://127.0.0.1:18080",
                POSTGRES_DSN,
                gateway_tls_context(),
            )
            uploader_token = await login_user(UPLOADER_ID)
            outsider_token = await login_user(OUTSIDER_ID)
            for tenant in tenant_aliases:
                await login_user(UPLOADER_ID, tenant)
            await eventually(
                lambda: (
                    gateway.ready(uploader_token)
                    if models
                    else gateway.execute(uploader_token, "query { __typename }")
                ),
                timeout_seconds=90.0,
                description="authenticated Gateway readiness",
            )
            await _authorize_fixture_principals(spicedb)
            yield GatewayScenario(
                environment, gateway, spicedb, uploader_token, outsider_token
            )
        finally:
            await gateway.close()


async def provision_fixture_administrator(
    scenario: GatewayScenario, user_id: str
) -> None:
    """Creates an administrator without running the unrelated retail ingestion."""
    result = await scenario.gateway.execute(
        scenario.uploader_token,
        """

        mutation CreateFixtureAdministrator($userId: String!) { administration {
          createUser(userId: $userId, isActive: true)
        }}
        """,
        {"userId": user_id},
    )
    assert _field(result, "administration").get("createUser") is True
    await scenario.spicedb.touch(
        RelationshipSpec("tenant", TENANT_ID, "administrator", "user", user_id)
    )
    await login_user(user_id)


async def revoke_fixture_admin(scenario: GatewayScenario) -> None:
    """Drops the uploader's bootstrap role while retaining their ordinary membership."""
    await scenario.spicedb.delete(
        RelationshipSpec(
            "tenant", TENANT_ID, "administrator", "user", UPLOADER_ID
        )
    )
