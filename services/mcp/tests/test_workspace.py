"""Workspace MCP tools preserve caller authority and explicit publication links."""

import json
import os
import unittest
from unittest.mock import patch

import httpx
from galadril_mcp_core.auth import MCPAuth
from galadril_mcp_core.client import delegated_call
from galadril_mcp_service.server import create_server
from mcp.server.auth.provider import AccessToken


class Verifier:
    resource = "https://mcp.example/workspace/mcp"
    scopes = [
        "workspace:use",
        "mail:read",
        "mail:draft",
        "mail:send",
        "documents:read",
        "documents:write",
    ]

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "fixture-token":
            return None
        return AccessToken(
            token=token,
            client_id="fixture-workspace",
            subject="fixture-user",
            scopes=self.scopes,
            resource=self.resource,
        )


class WorkspaceTest(unittest.IsolatedAsyncioTestCase):
    async def test_root_discovers_both_providers_and_checks_each_scope(
        self,
    ) -> None:
        verifier = Verifier()
        verifier.resource = "https://mcp.example/"
        verifier.scopes = ["workspace:use", "email:read"]
        result = await self.request("tools/list", {}, verifier=verifier)
        tools = {tool["name"]: tool for tool in result["result"]["tools"]}
        self.assertIn("read_email", tools)
        self.assertIn("verify_citation", tools)
        self.assertNotIn("invoke_tool", tools)
        self.assertIn(
            "email:read",
            tools["read_email"]["_meta"]["securitySchemes"][0]["scopes"],
        )
        read = await self.request(
            "tools/call",
            {
                "name": "read_email",
                "arguments": {"request": {"operation": "accounts"}},
            },
            verifier=verifier,
        )
        self.assertFalse(read["result"].get("isError", False))
        legal = await self.request(
            "tools/call",
            {
                "name": "search",
                "arguments": {"query": "vice de procédure"},
            },
            verifier=verifier,
        )
        self.assertTrue(legal["result"]["isError"])
        send = await self.request(
            "tools/call",
            {
                "name": "send_email",
                "arguments": {
                    "account": "a" * 32,
                    "draft": "b" * 32,
                    "revision": 1,
                },
            },
            verifier=verifier,
        )
        self.assertTrue(send["result"]["isError"])
        self.assertEqual(len(self.calls), 1)

    async def test_native_workspace_call_keeps_parent_authority_inside_gateway(
        self,
    ) -> None:
        parent, child = "a" * 64, "b" * 64

        class NativeVerifier:
            async def verify_token(self, token: str) -> AccessToken | None:
                return (
                    AccessToken(
                        token=token,
                        client_id="galadril-pengolo",
                        scopes=["workspace:use"],
                        subject="fixture-user",
                        resource="https://mcp.example/workspace/mcp",
                    )
                    if token == child
                    else None
                )

        def gateway(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tools":
                self.assertEqual(
                    request.headers["authorization"], "Bearer " + parent
                )
                return httpx.Response(
                    200, json={"token": child, "expires_in": 60}
                )
            self.assertEqual(request.url.path, "/internal/mcp/delegated")
            self.assertEqual(
                request.headers["authorization"], "Bearer " + child
            )
            self.assertEqual(
                json.loads(request.content),
                {
                    "project_id": None,
                    "request": {"operation": "search", "question": "fixture"},
                },
            )
            return httpx.Response(200, json={"evidence": []})

        original_client = httpx.AsyncClient
        async with original_client(
            transport=httpx.MockTransport(gateway)
        ) as http:
            settings = MCPAuth(
                issuer="https://issuer.example/",
                resource="https://mcp.example/workspace/mcp",
                jwks_url="https://issuer.example/jwks",
                scopes=("workspace:use",),
            )
            server = create_server(
                settings, http, provider="workspace", verifier=NativeVerifier()
            )
            app = server.streamable_http_app()

            def transport(**kwargs: object) -> httpx.AsyncClient:
                return original_client(
                    transport=httpx.ASGITransport(app), **kwargs
                )

            async with server.session_manager.run():
                with (
                    patch.dict(
                        os.environ,
                        {
                            "MCP_WORKSPACE_DELEGATED_URL": "http://gateway/internal/mcp/delegated"
                        },
                    ),
                    patch(
                        "galadril_mcp_core.client.httpx.AsyncClient",
                        side_effect=transport,
                    ),
                ):
                    result = await delegated_call(
                        http,
                        "http://gateway/tools",
                        parent,
                        capability_id="core.mcp.workspace",
                        resource=settings.resource,
                        endpoint=settings.resource,
                        tool="invoke_tool",
                        arguments={
                            "request": {
                                "operation": "search",
                                "question": "fixture",
                            }
                        },
                    )
            self.assertEqual(result, {"value": {"evidence": []}})

    async def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        verifier: Verifier | None = None,
    ) -> dict:
        verifier = verifier or Verifier()
        root = verifier.resource.endswith("/")

        async def gateway(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url, "http://gateway/internal/mcp/tools")
            self.assertEqual(
                request.headers["authorization"], "Bearer fixture-token"
            )
            payload = json.loads(request.content)
            self.calls.append(payload)
            self.assertNotIn("user", payload)
            self.assertNotIn("tenant", payload)
            return httpx.Response(200, json=self.response)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(gateway)
        ) as http:
            server = create_server(
                MCPAuth(
                    issuer="https://issuer.example/",
                    resource=verifier.resource,
                    jwks_url="https://issuer.example/jwks",
                    scopes=("workspace:use",),
                ),
                http,
                provider="unified" if root else "workspace",
                verifier=verifier,
            )
            app = server.streamable_http_app()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://mcp.example",
                    headers={
                        "authorization": "Bearer fixture-token",
                        "accept": "application/json, text/event-stream",
                    },
                ) as client:
                    result = await client.post(
                        "/" if root else "/workspace/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": method,
                            "params": params,
                        },
                    )
                    self.assertEqual(result.status_code, 200)
                    return result.json()

    async def asyncSetUp(self) -> None:
        self.calls: list[dict] = []
        self.response: dict = {"items": []}
        self.environment = patch.dict(
            os.environ,
            {
                "MCP_WORKSPACE_GATEWAY_URL": "http://gateway/internal/mcp/tools",
                "MCP_DOWNLOAD_ORIGIN": "https://galadril.example",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_discovery_is_side_effect_free_and_declares_write_semantics(
        self,
    ) -> None:
        result = await self.request("tools/list", {})
        tools = {item["name"]: item for item in result["result"]["tools"]}
        self.assertIn("compile_document", tools)
        self.assertIn("search_data", tools)
        self.assertNotIn("invoke_tool", tools)
        self.assertTrue(tools["read_email"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["send_email"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["send_email"]["annotations"]["destructiveHint"])
        self.assertEqual(
            tools["send_email"]["_meta"]["securitySchemes"],
            [{"type": "oauth2", "scopes": ["workspace:use", "mail:send"]}],
        )
        self.assertTrue(
            tools["document_templates"]["annotations"]["readOnlyHint"]
        )
        self.assertFalse(
            tools["manage_document_templates"]["annotations"]["readOnlyHint"]
        )
        self.assertIn("$defs", tools["compile_document"]["inputSchema"])
        self.assertFalse(self.calls)

    async def test_calls_forward_only_the_authenticated_transport_identity(
        self,
    ) -> None:
        result = await self.request(
            "tools/call",
            {
                "name": "read_email",
                "arguments": {"request": {"operation": "accounts"}},
            },
        )
        self.assertFalse(result["result"].get("isError", False))
        self.assertEqual(
            self.calls,
            [
                {
                    "project_id": None,
                    "request": {
                        "operation": "email",
                        "request": {"operation": "accounts"},
                    },
                }
            ],
        )

    async def test_read_tool_cannot_approve_or_send_a_draft(self) -> None:
        for operation in ("approve", "send", "reveal", "grant", "save_profile"):
            result = await self.request(
                "tools/call",
                {
                    "name": "read_email",
                    "arguments": {"request": {"operation": operation}},
                },
            )
            self.assertTrue(result["result"]["isError"])
        self.assertFalse(self.calls)

    async def test_sending_requests_review_without_approving_or_sending(
        self,
    ) -> None:
        self.response = {
            "status": "approval_required",
            "review_url": "https://galadril.example/mcp/email/review",
        }
        result = await self.request(
            "tools/call",
            {
                "name": "send_email",
                "arguments": {
                    "account": "a" * 32,
                    "draft": "b" * 32,
                    "revision": 7,
                },
            },
        )
        self.assertEqual(
            result["result"]["structuredContent"]["status"], "approval_required"
        )
        self.assertEqual(
            self.calls,
            [
                {
                    "project_id": None,
                    "request": {
                        "operation": "email_send",
                        "account": "a" * 32,
                        "draft": "b" * 32,
                        "revision": 7,
                    },
                }
            ],
        )

    async def test_drafts_attach_only_file_identifiers_through_trusted_gateway(
        self,
    ) -> None:
        self.response = {"id": "b" * 32, "revision": 1}
        result = await self.request(
            "tools/call",
            {
                "name": "save_email_draft",
                "arguments": {
                    "request": {
                        "account": "a" * 32,
                        "content": {
                            "to": ["recipient@example.test"],
                            "subject": "Fixture",
                            "body": "Review this draft.",
                        },
                    },
                    "file_ids": ["c" * 32, "d" * 32],
                },
            },
        )
        self.assertFalse(result["result"].get("isError", False))
        self.assertEqual(
            self.calls[0]["request"]["operation"], "email_save_draft"
        )
        self.assertEqual(
            self.calls[0]["request"]["file_ids"], ["c" * 32, "d" * 32]
        )
        self.assertEqual(
            self.calls[0]["request"]["request"]["content"]["attachments"], []
        )

    async def test_published_pdf_has_resource_link_and_text_without_storage_keys(
        self,
    ) -> None:
        self.response = {
            "file": {
                "id": "a" * 32,
                "name": "letter.pdf",
                "mime": "application/pdf",
            }
        }
        result = await self.request(
            "tools/call",
            {
                "name": "document_job",
                "arguments": {
                    "request": {
                        "action": "save",
                        "job_id": "b" * 64,
                        "name": "letter.pdf",
                    }
                },
            },
        )
        content = result["result"]["content"]
        url = "https://galadril.example/pengolo/files/" + "a" * 32
        self.assertTrue(
            any(
                item["type"] == "resource_link" and item["uri"] == url
                for item in content
            )
        )
        self.assertTrue(
            any(
                item["type"] == "text" and url in item["text"]
                for item in content
            )
        )


if __name__ == "__main__":
    unittest.main()
