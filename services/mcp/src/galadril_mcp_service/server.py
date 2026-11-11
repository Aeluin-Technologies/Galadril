"""Host separately authorized MCP namespaces through one supervised service."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
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
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.extension import Extension
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from mcp.types import Tool as MCPTool
from opentelemetry import trace
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
)
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from galadril_mcp_service.authorization import ToolScopeRequired
from galadril_mcp_service.providers.unified import SCOPES as UNIFIED_SCOPES
from galadril_mcp_service.providers.workspace import document_apps
from galadril_mcp_service.skills import PackagedSkills

DOCUMENT_WORKFLOW = (
    "To produce a PDF, use document_templates to find and read the authorized "
    "template, then compile_document with document.source containing the actual "
    "requested text for the document_source_target returned by the template. "
    "Use document_case_status to check project fields and private profile readiness. "
    "Poll with document_job_status, then save with document_job and return its "
    "download link. Compiling and saving a PDF do not send or file anything. "
    "For email, use read_email to identify the mailbox, contact and original "
    "thread, then save_email_draft. Sending is a separate confirmed action."
)
LEGAL_WORKFLOW = (
    "Use the recherche-jurisprudence and recherche-normes skills. Send only "
    "public legal terms. Run search_decisions with opposing solution filters "
    "and read full decisions and appeals with get_decision. Resolve dated norms "
    "with search_norms and get_norm. Use search and fetch for official sources "
    "and verify_citation to confirm each French citation against Légifrance."
)


class NamespaceServer(MCPServer[None]):
    def __init__(
        self,
        provider: str,
        settings: MCPAuth,
        verifier: TokenVerifier,
        request_body_limit: int,
    ) -> None:
        extensions: list[Extension] = [PackagedSkills(provider)]
        if provider in {"unified", "workspace"}:
            extensions.append(document_apps())
        super().__init__(
            provider,
            extensions=extensions,
            instructions={
                "unified": f"{LEGAL_WORKFLOW}\n\n{DOCUMENT_WORKFLOW}",
                "workspace": DOCUMENT_WORKFLOW,
                "piste": LEGAL_WORKFLOW,
            }.get(provider),
            subscriptions=False,
            token_verifier=verifier,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(settings.issuer),
                resource_server_url=AnyHttpUrl(settings.resource),
                required_scopes=list(settings.scopes),
                validate_token_resource=True,
            ),
        )
        self._resource = urlsplit(settings.resource)
        self._required_scopes = settings.scopes
        self._request_body_limit = request_body_limit

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
        context: Context[None, object] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """Let the client request additional consent for a verified caller."""
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as failure:
            error = failure.__cause__
            if not isinstance(error, ToolScopeRequired):
                raise
            trace.get_current_span().add_event("mcp.tool.insufficient_scope")
            path = self._resource.path.rstrip("/")
            metadata = f"https://{self._resource.netloc}/.well-known/oauth-protected-resource{path}"
            scopes = " ".join(
                dict.fromkeys((*self._required_scopes, error.scope))
            )
            challenge = (
                f"Bearer resource_metadata={json.dumps(metadata)}, "
                'error="insufficient_scope", '
                'error_description="Additional Galadril permissions are required", '
                f"scope={json.dumps(scopes)}"
            )
            return CallToolResult(
                content=[TextContent(type="text", text=str(error))],
                is_error=True,
                _meta={"mcp/www_authenticate": [challenge]},
            )

    def namespace_app(self) -> Starlette:
        """Apply the same audience, host and size policy to both protocol versions."""
        return self.streamable_http_app(
            stateless_http=True,
            json_response=True,
            streamable_http_path=self._resource.path,
            max_request_body_size=self._request_body_limit,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[self._resource.netloc],
                allowed_origins=["https://" + self._resource.netloc],
            ),
        )

    async def list_tools(self) -> list[MCPTool]:
        """Internal adapters are not part of the public workspace tool catalogue."""
        tools = await super().list_tools()
        if not any(tool.name == "invoke_tool" for tool in tools):
            return tools
        identity = get_access_token()
        native = (
            identity is not None and identity.client_id == "galadril-pengolo"
        )
        return [
            tool for tool in tools if (tool.name == "invoke_tool") == native
        ]


def create_server(
    settings: MCPAuth,
    http: httpx.AsyncClient,
    *,
    provider: str,
    verifier: TokenVerifier | None = None,
    request_body_limit: int = 65536,
) -> NamespaceServer:
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
    if not 65536 <= request_body_limit <= 1024 * 1024:
        raise ValueError("Invalid MCP request body limit")
    server = NamespaceServer(
        provider,
        settings,
        verifier or OAuthVerifier(settings, http),
        request_body_limit,
    )
    module = importlib.import_module(
        "galadril_mcp_service.providers." + provider
    )
    register = cast("RegisterProvider", module.register)
    register(server, http)
    return server


RegisterProvider = Callable[[MCPServer[None], httpx.AsyncClient], None]


class Namespace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    path: str = Field(pattern=r"^(?:/|/[a-z][a-z0-9_/-]{0,127}/mcp)$")
    scopes: tuple[str, ...] = Field(min_length=1, max_length=32)
    clients: tuple[str, ...] = Field(default=(), max_length=128)
    request_body_limit: int = Field(default=65536, ge=65536, le=1024 * 1024)


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
                settings,
                http,
                provider=namespace.provider,
                verifier=verifier,
                request_body_limit=namespace.request_body_limit,
            )
            app = server.namespace_app()
            apps.append(app)
            routes[namespace.path] = app
            metadata = "/.well-known/oauth-protected-resource"
            if namespace.path == "/":
                routes[metadata] = JSONResponse(
                    {
                        "resource": settings.resource,
                        "authorization_servers": [settings.issuer],
                        "scopes_supported": UNIFIED_SCOPES,
                        "bearer_methods_supported": ["header"],
                    },
                    headers={"Cache-Control": "no-store"},
                )
            else:
                routes[metadata + namespace.path] = app

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
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    with telemetry("galadril-mcp"):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
