"""MCP authentication applies to discovery as well as tool execution."""

import json
import os
import unittest
from typing import cast
from unittest.mock import patch

import httpx
from galadril_mcp_core.auth import MCPAuth
from galadril_mcp_core.client import (
    MCPUnavailable,
    _public_result,
    delegated_call,
)
from galadril_mcp_service.server import create_server, serve
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from uvicorn import Config


class _Verifier:
    def __init__(self, token: str = "fixture-token") -> None:
        self.token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        return (
            AccessToken(
                token=token,
                client_id="fixture",
                scopes=["legal:read"],
                resource="https://mcp.example/law/mcp",
            )
            if token == self.token
            else None
        )


class LawMCPTest(unittest.IsolatedAsyncioTestCase):
    def test_tool_errors_and_oversized_or_malformed_results_are_not_evidence(
        self,
    ) -> None:
        def text(value: str) -> TextContent:
            return TextContent(type="text", text=value)

        self.assertEqual(
            _public_result(
                CallToolResult(content=[text('{"value": "public"}')])
            ),
            {"value": "public"},
        )
        for result in (
            CallToolResult(content=[], isError=True),
            CallToolResult(content=[]),
            CallToolResult(content=[text("{}"), text("{}")]),
            CallToolResult(content=[text("x" * 262145)]),
            CallToolResult(content=[text("[]")]),
            CallToolResult(
                content=[], structuredContent={"value": "x" * 262144}
            ),
        ):
            with (
                self.subTest(result_size=len(result.content)),
                self.assertRaises(MCPUnavailable),
            ):
                _public_result(result)

    async def test_hub_bootstrap_keeps_namespace_resources_distinct(
        self,
    ) -> None:
        assertions = self

        class FixtureServer:
            def __init__(self, config: Config) -> None:
                self.app = cast(Starlette, config.app)

            async def serve(self) -> None:
                async with self.app.router.lifespan_context(self.app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(self.app),
                        base_url="https://mcp.example",
                    ) as http:
                        for prefix in ("law", "other"):
                            response = await http.get(
                                "/.well-known/oauth-protected-resource/"
                                + prefix
                                + "/mcp"
                            )
                            assertions.assertEqual(response.status_code, 200)
                            assertions.assertEqual(
                                response.json()["resource"],
                                "https://mcp.example/" + prefix + "/mcp",
                            )
                        assertions.assertEqual(
                            (await http.get("/unknown/mcp")).status_code, 404
                        )

        namespaces = [
            {
                "provider": "piste",
                "capability_id": "fixture." + name,
                "path": "/" + name + "/mcp",
                "scopes": ["legal:read"],
            }
            for name in ("law", "other")
        ]
        with (
            patch.dict(
                os.environ,
                {
                    "MCP_PUBLIC_ORIGIN": "https://mcp.example",
                    "MCP_NAMESPACES": json.dumps(namespaces),
                    "MCP_OAUTH_ISSUER": "https://auth.example",
                    "MCP_JWKS_URL": "https://auth.example/jwks",
                    "MCP_GATEWAY_TOOLS_URL": "https://gateway.example/tools",
                },
            ),
            patch("galadril_mcp_service.server.uvicorn.Server", FixtureServer),
        ):
            await serve()

    async def test_native_client_sends_only_the_attenuated_grant_to_mcp(
        self,
    ) -> None:
        parent, child = "a" * 64, "b" * 64
        settings = MCPAuth(
            issuer="https://auth.example",
            resource="https://mcp.example/law/mcp",
            jwks_url="https://auth.example/jwks",
            scopes=("legal:read",),
        )
        delegated: list[httpx.Request] = []

        def gateway(request: httpx.Request) -> httpx.Response:
            delegated.append(request)
            self.assertEqual(
                request.headers["authorization"], "Bearer " + parent
            )
            return httpx.Response(200, json={"token": child, "expires_in": 60})

        original_client = httpx.AsyncClient
        async with original_client(
            transport=httpx.MockTransport(gateway)
        ) as http:
            verifier = _Verifier(child)
            server = create_server(
                settings, http, provider="piste", verifier=verifier
            )

            @server.tool()
            def fixture_read() -> dict[str, str]:
                """Return public fixture data without contacting PISTE."""
                return {"value": "verified"}

            app = server.streamable_http_app()

            def transport(
                *, headers: dict[str, str], timeout: float, trust_env: bool
            ) -> httpx.AsyncClient:
                self.assertEqual(headers["authorization"], "Bearer " + child)
                return original_client(
                    transport=httpx.ASGITransport(app),
                    headers=headers,
                    timeout=timeout,
                    trust_env=trust_env,
                )

            async with server.session_manager.run():
                with patch(
                    "galadril_mcp_core.client.httpx.AsyncClient",
                    side_effect=transport,
                ):
                    result = await delegated_call(
                        http,
                        "https://gateway.example/tools",
                        parent,
                        capability_id="fixture.mcp",
                        resource=settings.resource,
                        endpoint=settings.resource,
                        tool="fixture_read",
                        arguments={},
                    )
                    verifier.token = "revoked"
                    with self.assertRaises(MCPUnavailable):
                        await delegated_call(
                            http,
                            "https://gateway.example/tools",
                            parent,
                            capability_id="fixture.mcp",
                            resource=settings.resource,
                            endpoint=settings.resource,
                            tool="fixture_read",
                            arguments={},
                        )
        self.assertEqual(result, {"value": "verified"})
        self.assertEqual(len(delegated), 2)

    async def test_unauthenticated_discovery_is_denied_and_metadata_is_public(
        self,
    ) -> None:
        settings = MCPAuth(
            issuer="https://auth.example",
            resource="https://mcp.example/law/mcp",
            jwks_url="https://auth.example/jwks",
            scopes=("legal:read",),
        )
        async with httpx.AsyncClient() as outgoing:
            server = create_server(
                settings, outgoing, provider="piste", verifier=_Verifier()
            )
            app = server.streamable_http_app()
            async with (
                server.session_manager.run(),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="https://mcp.example",
                ) as http,
            ):
                response = await http.post(
                    "/law/mcp",
                    json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
                )
                self.assertEqual(response.status_code, 401)
                self.assertIn(
                    "resource_metadata", response.headers["www-authenticate"]
                )
                metadata = await http.get(
                    "/.well-known/oauth-protected-resource/law/mcp"
                )
                self.assertEqual(metadata.status_code, 200)
                self.assertEqual(metadata.json()["resource"], settings.resource)
                listed = await http.post(
                    "/law/mcp",
                    headers={
                        "authorization": "Bearer fixture-token",
                        "accept": "application/json, text/event-stream",
                    },
                    json={"jsonrpc": "2.0", "method": "tools/list", "id": 2},
                )
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(
                    {tool["name"] for tool in listed.json()["result"]["tools"]},
                    {"search", "fetch", "verify_citation"},
                )
            names = {tool.name for tool in await server.list_tools()}
            self.assertEqual(names, {"search", "fetch", "verify_citation"})


if __name__ == "__main__":
    unittest.main()
