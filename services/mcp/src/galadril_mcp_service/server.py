"""Host separately authorized MCP namespaces through one supervised service."""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import cast
from urllib.parse import urlsplit

import httpx
import uvicorn
from galadril_mcp_core.auth import GatewayVerifier, MCPAuth, OAuthVerifier
from galadril_plugins.telemetry import telemetry
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, TypeAdapter
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send


def create_server(
    settings: MCPAuth,
    http: httpx.AsyncClient,
    *,
    provider: str,
    verifier: TokenVerifier | None = None,
) -> FastMCP:
    """Create an authenticated namespace from operator-owned provider configuration.

    Args:
        settings: Canonical issuer, resource, scopes and signing-key location.
        http: Shared fixed-policy client used by the installed provider.
        provider: Installed provider name, never taken from model arguments.
        verifier: Optional authority verifier for native delegated calls.

    Returns:
        A stateless MCP server bound to the exact resource path and audience.
    """
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", provider) is None:
        raise ValueError("Invalid installed MCP provider")
    host = urlsplit(settings.resource).netloc
    server = FastMCP(
        provider,
        stateless_http=True,
        json_response=True,
        streamable_http_path=urlsplit(settings.resource).path,
        max_request_body_size=65536,
        token_verifier=verifier or OAuthVerifier(settings, http),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(settings.resource),
            required_scopes=list(settings.scopes),
            validate_token_resource=True,
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host],
            allowed_origins=["https://" + host],
        ),
    )
    module = importlib.import_module(
        "galadril_mcp_service.providers." + provider
    )
    register = cast("RegisterProvider", module.register)
    register(server, http)
    return server


RegisterProvider = Callable[[FastMCP, httpx.AsyncClient], None]


class Namespace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    path: str = Field(pattern=r"^/[a-z][a-z0-9_/-]{0,127}/mcp$")
    scopes: tuple[str, ...] = Field(min_length=1, max_length=32)
    clients: tuple[str, ...] = Field(default=(), max_length=128)


class Hub:
    """Dispatch the original path so each namespace retains its OAuth audience."""

    __slots__ = ("routes",)

    def __init__(self, routes: dict[str, ASGIApp]) -> None:
        self.routes = routes

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        app = self.routes.get(scope.get("path", ""))
        if app is None:
            await Response(status_code=404)(scope, receive, send)
            return
        await app(scope, receive, send)


def client_tls_context() -> ssl.SSLContext:
    """Internal JWKS discovery needs workload trust without weakening public TLS verification."""
    context = ssl.create_default_context()
    authority = os.environ.get("MCP_TLS_CA_FILE")
    if authority:
        context.load_verify_locations(cafile=authority)
    return context


async def serve() -> None:
    """Keep namespace lifespans and their shared HTTP client under one supervisor."""
    origin = os.environ["MCP_PUBLIC_ORIGIN"].rstrip("/")
    namespaces = TypeAdapter(tuple[Namespace, ...]).validate_json(
        os.environ["MCP_NAMESPACES"]
    )
    if (
        not namespaces
        or len(namespaces) > 32
        or len({item.path for item in namespaces}) != len(namespaces)
    ):
        raise ValueError("MCP namespaces must be bounded and unique")
    async with httpx.AsyncClient(
        trust_env=False, verify=client_tls_context()
    ) as http:
        apps: list[Starlette] = []
        routes: dict[str, ASGIApp] = {}
        for namespace in namespaces:
            settings = MCPAuth(
                issuer=os.environ["MCP_OAUTH_ISSUER"],
                resource=origin + namespace.path,
                jwks_url=os.environ["MCP_JWKS_URL"],
                scopes=namespace.scopes,
                clients=namespace.clients,
            )
            verifier = GatewayVerifier(
                settings,
                http,
                os.environ["MCP_GATEWAY_TOOLS_URL"],
                namespace.capability_id,
            )
            server = create_server(
                settings, http, provider=namespace.provider, verifier=verifier
            )
            app = server.streamable_http_app()
            apps.append(app)
            routes[namespace.path] = app
            routes["/.well-known/oauth-protected-resource" + namespace.path] = (
                app
            )

        @asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            async with AsyncExitStack() as stack:
                for application in apps:
                    await stack.enter_async_context(
                        application.router.lifespan_context(application)
                    )
                yield

        application = Starlette(
            routes=[Mount("/", app=Hub(routes))], lifespan=lifespan
        )
        process = uvicorn.Server(
            uvicorn.Config(
                application,
                host=os.environ.get("MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("MCP_PORT", "8097")),
                access_log=False,
                log_level="warning",
                proxy_headers=False,
            )
        )
        await process.serve()


def main() -> None:
    """Initialize OTLP before accepting requests or loading provider credentials."""
    with telemetry("galadril-mcp"):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
