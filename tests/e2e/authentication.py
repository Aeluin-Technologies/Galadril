"""Real browser login sessions shared by the end-to-end scenario actors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import psycopg
from assertions import require_mapping

from infrastructure.auth.tests.webauthn import VirtualAuthenticator

PrivateRequest = Callable[
    [str, Mapping[str, object] | None, str], Awaitable[Mapping[str, object]]
]
_tokens: dict[tuple[str, str], str] = {}
_sessions: AuthenticationSessions | None = None


def authenticated_token(user: str, *, tenant_id: str = "debug_tenant") -> str:
    """Returns only a Hydra token obtained through a previous browser login."""
    token = _tokens.get((tenant_id, user))
    if token is None:
        raise RuntimeError(f"Principal {user} has not authenticated")
    return token


def initialize_authentication(
    private: PrivateRequest, origin: str, dsn: str, context: ssl.SSLContext
) -> None:
    """Scopes cached sessions to one isolated Compose environment."""
    global _sessions
    _tokens.clear()
    _sessions = AuthenticationSessions(private, origin, dsn, context)


async def login_user(user: str, tenant: str = "debug_tenant") -> str:
    """Authenticates an existing canonical IAM user without granting rights."""
    if _sessions is None:
        raise RuntimeError("Authentication environment is not initialized")
    token = await _sessions.login(user, tenant)
    _tokens[(tenant, user)] = token
    return token


async def expired_token(user: str) -> str:
    """Exercises expiration with a genuine short-lived Hydra signature."""
    if _sessions is None:
        raise RuntimeError("Authentication environment is not initialized")
    url = "http://127.0.0.1:4445/admin/clients/galadril-studio"
    original = await _sessions.private(url, None, "GET")
    await _sessions.private(
        url,
        dict(original)
        | {"authorization_code_grant_access_token_lifespan": "3s"},
        "PUT",
    )
    try:
        token = await _sessions.login(user, "debug_tenant")
    finally:
        await _sessions.private(url, original, "PUT")
    claims = token_claims(token)
    remaining = max(0.0, float(str(claims["exp"])) - time.time())
    if remaining > 5.0:
        raise AssertionError(
            "Hydra did not apply the short client token lifetime"
        )
    await asyncio.sleep(remaining + 0.2)
    return token


def token_claims(token: str) -> Mapping[str, object]:
    """Inspects issued claims; Envoy independently verifies the signature."""
    segment = token.split(".")[1]
    return require_mapping(
        json.loads(
            base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        ),
        "access claims",
    )


class AuthenticationSessions:
    """Drives the shipped browser flow, including Kratos CSRF and Hydra PKCE."""

    __slots__ = (
        "private",
        "origin",
        "dsn",
        "context",
        "identities",
        "next_login",
        "authenticators",
    )

    def __init__(
        self,
        private: PrivateRequest,
        origin: str,
        dsn: str,
        context: ssl.SSLContext,
    ) -> None:
        self.private, self.origin, self.dsn, self.context = (
            private,
            origin,
            dsn,
            context,
        )
        self.identities: dict[tuple[str, str], tuple[str, str]] = {}
        self.next_login = 0.0
        self.authenticators: dict[tuple[str, str], VirtualAuthenticator] = {}

    async def provision(self, user: str, tenant: str) -> tuple[str, str]:
        key = (tenant, user)
        if key in self.identities:
            return self.identities[key]
        email = (
            hashlib.sha256(json.dumps(key).encode()).hexdigest()
            + "@e2e.invalid"
        )
        password = secrets.token_urlsafe(32)
        identity = await self.private(
            "http://127.0.0.1:4434/admin/identities",
            {
                "schema_id": "local",
                "traits": {"email": email},
                "metadata_admin": {
                    "galadril": {"tenant_id": tenant, "user_id": user}
                },
                "credentials": {"password": {"config": {"password": password}}},
            },
            "POST",
        )
        async with await psycopg.AsyncConnection.connect(
            self.dsn
        ) as connection:
            await connection.execute(
                "INSERT INTO sso_tenant_settings(tenant_id,passkey_mfa) VALUES(%s,true) ON CONFLICT(tenant_id) DO UPDATE SET passkey_mfa=true",
                (tenant,),
            )
            await connection.execute(
                "INSERT INTO iam_external_identities(tenant_id,connection_id,external_subject,user_id) VALUES(%s,'kratos',%s,%s)",
                (tenant, identity["id"], user),
            )
        self.identities[key] = (email, password)
        return email, password

    async def login(self, user: str, tenant: str) -> str:
        # Real users share a NAT address; honor the production login rate limit.
        await asyncio.sleep(max(0.0, self.next_login - time.monotonic()))
        self.next_login = time.monotonic() + 12.0
        email, password = await self.provision(user, tenant)
        async with httpx.AsyncClient(verify=self.context, timeout=10) as client:
            response = await client.post(
                self.origin + "/auth/start",
                headers={"Origin": self.origin},
                data={"email": email},
            )
            for _step in range(25):
                if response.status_code not in (302, 303):
                    raise AssertionError(
                        f"Login step {response.request.url.path} failed with HTTP {response.status_code}"
                    )
                target = urljoin(self.origin, response.headers["location"])
                if urlsplit(target).netloc != urlsplit(self.origin).netloc:
                    raise AssertionError(
                        "Login redirected outside the trusted origin"
                    )
                if target == self.origin + "/login?continue=1":
                    raise AssertionError(
                        "Email discovery requested a second login page"
                    )
                if urlsplit(target).path == "/login" and "flow" in parse_qs(
                    urlsplit(target).query
                ):
                    flow = parse_qs(urlsplit(target).query)["flow"][0]
                    ui_response = await client.get(
                        self.origin + "/auth/local/flow", params={"flow": flow}
                    )
                    ui_response.raise_for_status()
                    ui = require_mapping(ui_response.json(), "login form")
                    nodes = ui["nodes"]
                    if not isinstance(nodes, list):
                        raise TypeError("Expected login inputs")
                    fields: dict[str, str] = {}
                    for node in nodes:
                        attributes = require_mapping(
                            require_mapping(node, "node")["attributes"],
                            "attributes",
                        )
                        if attributes.get("type") == "hidden":
                            fields[str(attributes["name"])] = str(
                                attributes["value"]
                            )
                    fields.update(
                        identifier=email, password=password, method="password"
                    )
                    response = await client.post(
                        str(ui["action"]),
                        data=fields,
                        headers={"Origin": self.origin},
                    )
                elif target == self.origin + "/login?mfa=1":
                    if client.cookies.get("__Host-galadril-token"):
                        raise AssertionError("Access token issued before MFA")
                    options = await client.get(
                        self.origin + "/auth/mfa/options"
                    )
                    options.raise_for_status()
                    step = require_mapping(options.json(), "passkey step")
                    authenticator = self.authenticators.setdefault(
                        (tenant, user), VirtualAuthenticator()
                    )
                    response = await client.post(
                        self.origin + "/auth/mfa/verify",
                        headers={"Origin": self.origin},
                        json=authenticator.respond(step, self.origin),
                    )
                elif target == self.origin + "/":
                    token = client.cookies.get("__Host-galadril-token")
                    if not token:
                        raise AssertionError(
                            "Login did not set the access cookie"
                        )
                    claims = token_claims(token)
                    assert (
                        claims["sub"] == user and claims["tenant_id"] == tenant
                    )
                    assert claims["token_use"] == "access"
                    assert claims["acr"] == "urn:galadril:webauthn:uv"
                    assert isinstance(claims["mfa_generation"], int)
                    assert claims["iss"] == "https://aeluin.gravitalia.com"
                    assert (
                        claims["aud"] == ["galadril"]
                        or claims["aud"] == "galadril"
                    )
                    session = await client.get(self.origin + "/auth/session")
                    session.raise_for_status()
                    return token
                else:
                    response = await client.get(target)
        raise AssertionError("Login exceeded its redirect budget")
