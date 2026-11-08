"""Share OAuth resource verification across independently deployed MCP servers."""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.auth.provider import AccessToken
from opentelemetry import trace
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class MCPAuth(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    issuer: str
    resource: str
    jwks_url: str
    scopes: tuple[str, ...] = Field(min_length=1, max_length=32)
    clients: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def secure_urls(self) -> MCPAuth:
        """Pin resource identities to HTTPS origins without credential or query aliases."""
        for value in (self.issuer, self.resource, self.jwks_url):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError(
                    "MCP OAuth endpoints require canonical HTTPS URLs"
                )
            if any(
                (
                    parsed.username,
                    parsed.password,
                    parsed.query,
                    parsed.fragment,
                )
            ):
                raise ValueError(
                    "MCP OAuth endpoints require canonical HTTPS URLs"
                )
        if any(
            not scope
            or len(scope) > 128
            or any(char.isspace() for char in scope)
            for scope in self.scopes
        ):
            raise ValueError("Invalid MCP scope")
        return self


class OAuthVerifier:
    """Validate JWTs locally; only the configured issuer controls signing keys."""

    __slots__ = ("settings", "http", "keys", "expires", "refresh_after", "lock")

    def __init__(self, settings: MCPAuth, http: httpx.AsyncClient) -> None:
        self.settings, self.http = settings, http
        self.keys: dict[str, jwt.PyJWK] = {}
        self.expires = self.refresh_after = 0.0
        self.lock = asyncio.Lock()

    async def _fetch_keys(self) -> dict[str, jwt.PyJWK]:
        """Bound key downloads and accept only signing keys from the configured issuer."""
        async with self.http.stream(
            "GET",
            self.settings.jwks_url,
            timeout=10,
            follow_redirects=False,
            auth=None,
        ) as response:
            if response.status_code != 200:
                return {}
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 262144:
                    return {}
                body.extend(chunk)
        decoded = jwt.PyJWKSet.from_json(body.decode())
        if not 1 <= len(decoded.keys) <= 64:
            return {}
        return {key.key_id: key for key in decoded.keys if _signing_key(key)}

    async def _key(self, identifier: str) -> jwt.PyJWK | None:
        """Share a short-lived JWKS cache and rate-limit unknown-key refreshes."""
        now = time.monotonic()
        if now < self.expires and identifier in self.keys:
            return self.keys[identifier]
        async with self.lock:
            now = time.monotonic()
            if now < self.refresh_after:
                return self.keys.get(identifier) if now < self.expires else None
            self.refresh_after = now + 30
            self.keys = await self._fetch_keys()
            self.expires = now + 300
            return self.keys.get(identifier)

    async def _verified(self, token: str) -> AccessToken | None:
        """Verify signature, issuer and resource before interpreting identity claims."""
        kid = _key_id(token)
        if kid is None:
            return None
        key = await self._key(kid)
        if key is None:
            return None
        claims = _Claims.model_validate(
            jwt.decode(
                token,
                key.key,
                algorithms=[key.algorithm_name],
                issuer=self.settings.issuer,
                audience=self.settings.resource,
                options={"require": ["iss", "aud", "exp", "sub"]},
            )
        )
        if not set(self.settings.scopes) <= set(claims.scopes):
            return None
        if self.settings.clients and claims.client not in self.settings.clients:
            return None
        return AccessToken(
            token=token,
            client_id=claims.client,
            scopes=claims.scopes,
            expires_at=claims.expires,
            subject=claims.subject,
            resource=self.settings.resource,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Fail closed and trace only the outcome, never tokens or claim contents."""
        with trace.get_tracer("galadril.mcp").start_as_current_span(
            "mcp.oauth.verify",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                identity = await self._verified(token)
            except (jwt.PyJWTError, httpx.HTTPError, UnicodeError, ValueError):
                identity = None
            span.set_attribute("mcp.auth.authorized", identity is not None)
            return identity


def _key_id(token: str) -> str | None:
    """Read a bounded key selector; untrusted header URLs never influence I/O."""
    if not 1 <= len(token) <= 16384:
        return None
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not isinstance(kid, str) or len(kid) > 256:
        return None
    if header.get("alg") not in {"RS256", "ES256"}:
        return None
    return kid


def _signing_key(key: jwt.PyJWK) -> bool:
    """Reject encryption keys and algorithms outside the server's allowlist."""
    return (
        bool(key.key_id)
        and key.algorithm_name in {"RS256", "ES256"}
        and key.public_key_use in {None, "sig"}
    )


class _Claims(BaseModel):
    model_config = ConfigDict(
        extra="ignore", strict=True, hide_input_in_errors=True
    )
    subject: str = Field(alias="sub", min_length=1, max_length=256)
    expires: int = Field(alias="exp")
    client: str = Field(
        validation_alias=AliasChoices("client_id", "azp"),
        min_length=1,
        max_length=128,
    )
    scopes: list[str] = Field(
        validation_alias=AliasChoices("scope", "scp"), max_length=32
    )

    @field_validator("scopes", mode="before")
    @classmethod
    def split_scopes(cls, value: object) -> object:
        """Support the two issuer conventions without coercing malformed claims."""
        return value.split() if isinstance(value, str) else value


class GatewayVerifier:
    """Native calls use attenuated grants; public clients use resource-bound OAuth."""

    __slots__ = ("settings", "http", "gateway", "capability_id", "oauth")

    def __init__(
        self,
        settings: MCPAuth,
        http: httpx.AsyncClient,
        gateway: str,
        capability_id: str,
    ) -> None:
        self.settings, self.http, self.gateway, self.capability_id = (
            settings,
            http,
            gateway,
            capability_id,
        )
        self.oauth = OAuthVerifier(settings, http)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Reauthorize native grants on every call so revocation is immediately effective."""
        if re.fullmatch(r"[0-9a-f]{64}", token) is None:
            return await self.oauth.verify_token(token)
        try:
            return await self._native(token)
        except (httpx.HTTPError, ValueError):
            return None

    async def _native(self, token: str) -> AccessToken | None:
        """Bind a live Gateway decision to this namespace, without caching identity."""
        response = await self.http.post(
            self.gateway,
            headers={"authorization": "Bearer " + token},
            json={
                "operation": "mcp_authorize",
                "capability_id": self.capability_id,
                "resource": self.settings.resource,
            },
            timeout=10,
        )
        if response.status_code != 200 or len(response.content) > 4096:
            return None
        identity = _DelegationIdentity.model_validate_json(response.content)
        if (
            not identity.active
            or identity.resource != self.settings.resource
            or not set(self.settings.scopes) <= set(identity.scopes)
        ):
            return None
        trace.get_current_span().add_event(
            "mcp.user.authorized",
            {
                "mcp.id": self.capability_id,
                "generation.id": identity.generation,
            },
        )
        return AccessToken(
            token=token,
            client_id="galadril-pengolo",
            subject=identity.subject,
            scopes=identity.scopes,
            expires_at=int(time.time()) + identity.expires_in,
            resource=identity.resource,
        )


class _DelegationIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    active: bool
    subject: str = Field(min_length=1, max_length=256)
    tenant: str = Field(min_length=1, max_length=128)
    generation: str = Field(min_length=1, max_length=64)
    resource: str = Field(max_length=2048)
    scopes: list[str] = Field(max_length=32)
    expires_in: int = Field(ge=1, le=90)
