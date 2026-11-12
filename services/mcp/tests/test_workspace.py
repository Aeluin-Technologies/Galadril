"""Workspace MCP tools preserve caller authority and explicit publication links."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import httpx
import httpx2
from galadril_mcp_core.auth import MCPAuth
from galadril_mcp_core.client import delegated_call
from galadril_mcp_service.server import create_server
from mcp.server.auth.provider import AccessToken
from starlette.types import Receive, Scope, Send


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
    def test_default_plugin_uses_galadril_app_and_english_label(self) -> None:
        from galadril_mcp_service.package_plugin import package_plugin

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "galadril.zip"
            package_plugin(archive)
            with ZipFile(archive) as plugin:
                app = next(
                    iter(json.loads(plugin.read(".app.json"))["apps"].values())
                )
                self.assertEqual(
                    app["id"], "asdk_app_6af5f40dc3208191b60534eb3ea28b75"
                )
                for name in ("plugin.json", ".codex-plugin/plugin.json"):
                    manifest = json.loads(plugin.read(name))
                    interface = (
                        manifest
                        if name.startswith(".")
                        else manifest["extensions"]["com.openai"]
                    )["interface"]
                    self.assertEqual(
                        interface["shortDescription"],
                        "Legal research, email and LaTeX documents",
                    )

    def test_chatgpt_bundle_keeps_both_manifests_and_hosted_app(self) -> None:
        from galadril_mcp_service.package_plugin import package_plugin

        suffix = "a" * 32
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "galadril.zip"
            package_plugin(archive, chatgpt_app_id=f"plugin_asdk_app_{suffix}")
            with ZipFile(archive) as plugin:
                self.assertEqual(
                    json.loads(plugin.read(".app.json")),
                    {"apps": {f"dev-{suffix}": {"id": f"asdk_app_{suffix}"}}},
                )
                portable = json.loads(plugin.read("plugin.json"))
                native = json.loads(plugin.read(".codex-plugin/plugin.json"))
                self.assertEqual(
                    portable["extensions"]["com.openai"]["apps"], "./.app.json"
                )
                self.assertEqual(native["apps"], "./.app.json")
                self.assertEqual(native["mcpServers"], "./.mcp.json")
                servers = json.loads(plugin.read("mcp.json"))["mcpServers"]
                self.assertEqual(
                    json.loads(plugin.read(".mcp.json")),
                    {"mcpServers": servers},
                )
                server = servers["galadril"]
                self.assertEqual(
                    server["extensions"]["com.openai"]["auth"]["resource"],
                    server["url"],
                )

    def test_portable_plugin_bundles_both_legal_procedures(self) -> None:
        from galadril_mcp_service.package_plugin import package_plugin

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "galadril.zip"
            package_plugin(archive, chatgpt_app_id=None)
            with ZipFile(archive) as plugin:
                self.assertIsNone(plugin.testzip())
                self.assertEqual(
                    json.loads(plugin.read("plugin.json"))["name"], "galadril"
                )
                config = json.loads(plugin.read("mcp.json"))["mcpServers"][
                    "galadril"
                ]
                self.assertEqual(
                    config["extensions"]["com.openai"]["auth"]["type"], "oauth"
                )
                for name, reference in (
                    ("recherche-jurisprudence", "procedures.md"),
                    ("recherche-normes", "traps.md"),
                ):
                    self.assertIn(f"skills/{name}/SKILL.md", plugin.namelist())
                    self.assertIn(
                        f"skills/{name}/references/{reference}",
                        plugin.namelist(),
                    )
                    skill = "skills/galadril-legal-research"
                    procedure = f"references/{name}/procedure.md"
                    self.assertIn(
                        procedure, plugin.read(f"{skill}/SKILL.md").decode()
                    )
                    self.assertEqual(
                        plugin.read(f"{skill}/{procedure}"),
                        plugin.read(f"skills/{name}/SKILL.md"),
                    )
                    self.assertEqual(
                        plugin.read(
                            f"{skill}/references/{name}/references/{reference}"
                        ),
                        plugin.read(f"skills/{name}/references/{reference}"),
                    )
                self.assertIn(
                    "licenses/librejustice/LICENSE.md", plugin.namelist()
                )

    async def test_modern_discovery_and_skills_use_the_served_bytes(
        self,
    ) -> None:
        verifier = Verifier()
        verifier.resource = "https://mcp.example/"
        discovery = await self.request(
            "server/discover", {}, verifier=verifier, modern=True
        )
        extensions = discovery["result"]["capabilities"]["extensions"]
        self.assertIn("io.modelcontextprotocol/skills", extensions)
        self.assertIn("io.modelcontextprotocol/ui", extensions)
        result = await self.request(
            "skills/list", {}, verifier=verifier, modern=True
        )
        skills = {
            skill["frontmatter"]["name"]: skill
            for skill in result["result"]["skills"]
        }
        jurisprudence = skills["galadril-legal-research"]
        self.assertIn(
            "skill://galadril/galadril-legal-research/references/recherche-jurisprudence/procedure.md",
            {file["uri"] for file in jurisprudence["resources"]},
        )
        detail = await self.request(
            "skills/get",
            {"uri": jurisprudence["uri"]},
            verifier=verifier,
            modern=True,
        )
        self.assertEqual(detail["result"]["skill"], jurisprudence)
        for reference in jurisprudence["resources"]:
            result = await self.request(
                "resources/read", {"uri": reference["uri"]}, verifier=verifier
            )
            content = result["result"]["contents"][0]["text"].encode()
            self.assertEqual(reference["size"], len(content))
            self.assertEqual(
                reference["digest"],
                "sha256:" + hashlib.sha256(content).hexdigest(),
            )

    async def test_document_job_view_preserves_the_text_tool_result(
        self,
    ) -> None:
        catalog = await self.request("tools/list", {})
        tool = next(
            tool
            for tool in catalog["result"]["tools"]
            if tool["name"] == "document_job_status"
        )
        uri = tool["_meta"]["ui"]["resourceUri"]
        view = await self.request("resources/read", {"uri": uri})
        content = view["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("ui/notifications/tool-result", content["text"])
        self.assertIn("textContent", content["text"])
        self.assertEqual(self.calls, [])
        self.response = {"status": "running"}
        result = await self.request(
            "tools/call",
            {"name": "document_job_status", "arguments": {"job_id": "a" * 64}},
        )
        self.assertEqual(result["result"]["structuredContent"], self.response)
        self.assertEqual(result["result"]["content"][0]["type"], "text")

    async def test_root_advertises_workflows_before_any_tool_call(self) -> None:
        verifier = Verifier()
        verifier.resource = "https://mcp.example/"
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "1"},
            },
            verifier=verifier,
        )
        instructions = result["result"]["instructions"]
        for name in (
            "document.source",
            "document_job",
            "verify_citation",
            "read_email",
        ):
            self.assertIn(name, instructions)
        self.assertEqual(self.calls, [])

    async def test_root_serves_packaged_skills_as_resources(self) -> None:
        verifier = Verifier()
        verifier.resource = "https://mcp.example/"
        result = await self.request("resources/list", {}, verifier=verifier)
        uris = {
            item["uri"]
            for item in result["result"]["resources"]
            if item["uri"].startswith("skill://")
            and item["uri"].endswith(".md")
        }
        self.assertTrue(
            {
                "skill://galadril/galadril-legal-research/SKILL.md",
                "skill://galadril/galadril-email/SKILL.md",
                "skill://galadril/galadril-latex/SKILL.md",
                "skill://galadril/galadril-latex/references/compilation.md",
                "skill://galadril/galadril-legal-research/references/corpus.md",
                "skill://galadril/galadril-email/references/mail.md",
            }
            <= uris,
        )
        for uri in uris:
            result = await self.request(
                "resources/read", {"uri": uri}, verifier=verifier
            )
            expected = "description:" if uri.endswith("/SKILL.md") else "# "
            self.assertIn(expected, result["result"]["contents"][0]["text"])
        self.assertEqual(self.calls, [])

    async def test_template_read_exposes_the_discovered_source_target(
        self,
    ) -> None:
        self.response = {
            "entrypoint": "custom-entry.tex",
            "files": [{"path": "custom-entry.tex"}],
            "document_source_target": "custom-entry.tex",
            "server_bound_variables": [],
        }
        result = await self.request(
            "tools/call",
            {
                "name": "document_templates",
                "arguments": {
                    "request": {
                        "action": "read",
                        "template_id": "a" * 32,
                        "version": "b" * 64,
                    }
                },
            },
        )
        self.assertFalse(result["result"].get("isError", False))
        self.assertEqual(result["result"]["structuredContent"], self.response)

    async def test_invalid_compile_arguments_are_recoverable_tool_errors(
        self,
    ) -> None:
        result = await self.request(
            "tools/call",
            {
                "name": "compile_document",
                "arguments": {
                    "request": {
                        "template_id": "a" * 32,
                        "version": "b" * 64,
                        "idempotency_key": "not-a-uuid",
                        "document": {"source": "fixture"},
                    },
                },
            },
            modern=True,
        )
        self.assertTrue(result["result"]["isError"])
        self.assertIn("idempotency_key", result["result"]["content"][0]["text"])
        self.assertEqual(self.calls, [])

    async def test_workspace_propagates_trace_context_without_baggage(
        self,
    ) -> None:
        await self.request(
            "tools/call",
            {
                "_meta": {
                    "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
                    "baggage": "private=fixture",
                },
                "name": "read_email",
                "arguments": {"request": {"operation": "accounts"}},
            },
        )
        headers = self.gateway_headers[-1]
        self.assertEqual(
            headers["traceparent"].split("-")[1],
            "0af7651916cd43dd8448eb211c80319c",
        )
        self.assertNotIn("baggage", headers)

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
        for name in ("document_case_status", "document_job_status"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
        self.assertFalse(
            tools["compile_document"]["annotations"]["openWorldHint"]
        )
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
        [challenge] = legal["result"]["_meta"]["mcp/www_authenticate"]
        self.assertIn('error="insufficient_scope"', challenge)
        self.assertIn('scope="workspace:use legal:read"', challenge)
        self.assertIn(
            'resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"',
            challenge,
        )
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
        self.assertIn(
            'scope="workspace:use email:write"',
            send["result"]["_meta"]["mcp/www_authenticate"][0],
        )
        self.assertEqual(len(self.calls), 1)

    async def test_signature_request_returns_only_a_review_link(self) -> None:
        file_id = "a" * 32
        self.response = {
            "status": "signature_required",
            "review_url": f"https://galadril.example/mcp/sign/{file_id}",
        }
        request = {
            "name": "request_document_signature",
            "arguments": {"file_id": file_id},
        }
        result = await self.request("tools/call", request)
        self.assertFalse(result["result"].get("isError", False))
        self.assertEqual(result["result"]["structuredContent"], self.response)
        self.assertEqual(
            self.calls,
            [
                {
                    "project_id": None,
                    "request": {
                        "operation": "request_document_signature",
                        "file_id": file_id,
                    },
                }
            ],
        )
        verifier = Verifier()
        verifier.scopes = ["workspace:use"]
        denied = await self.request("tools/call", request, verifier=verifier)
        self.assertTrue(denied["result"]["isError"])
        self.assertEqual(len(self.calls), 1)

    async def test_document_preflight_and_poll_use_read_actions_only(
        self,
    ) -> None:
        project = "a" * 32
        for name, args, expected in (
            (
                "document_case_status",
                {"template_id": "b" * 32},
                {
                    "operation": "document_templates",
                    "request": {
                        "action": "document_case",
                        "template_id": "b" * 32,
                    },
                },
            ),
            (
                "document_job_status",
                {"job_id": "c" * 64},
                {
                    "operation": "document_job",
                    "request": {"action": "status", "job_id": "c" * 64},
                },
            ),
        ):
            with self.subTest(name=name):
                result = await self.request(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": {**args, "project_id": project},
                    },
                )
                self.assertFalse(result["result"].get("isError", False))
                self.assertEqual(
                    self.calls[-1], {"project_id": project, "request": expected}
                )

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
            app = server.namespace_app()

            methods: list[str] = []

            async def observe(
                scope: Scope, receive: Receive, send: Send
            ) -> None:
                headers = dict(scope["headers"])
                if method := headers.get(b"mcp-method"):
                    methods.append(method.decode())
                await app(scope, receive, send)

            mcp_client = httpx2.AsyncClient

            def transport(**kwargs: object) -> httpx2.AsyncClient:
                return mcp_client(
                    transport=httpx2.ASGITransport(observe), **kwargs
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
                        "galadril_mcp_core.client.httpx2.AsyncClient",
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
            self.assertEqual(methods[:2], ["server/discover", "tools/call"])
            self.assertEqual(methods.count("tools/call"), 1)

    async def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        verifier: Verifier | None = None,
        modern: bool = False,
    ) -> dict:
        verifier = verifier or Verifier()
        root = verifier.resource.endswith("/")

        async def gateway(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url, "http://gateway/internal/mcp/tools")
            self.assertEqual(
                request.headers["authorization"], "Bearer fixture-token"
            )
            self.gateway_headers.append(request.headers)
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
            app = server.namespace_app()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://mcp.example",
                    headers={
                        "authorization": "Bearer fixture-token",
                        "accept": "application/json, text/event-stream",
                    },
                ) as client:
                    headers: dict[str, str] = {}
                    if modern:
                        headers = {
                            "MCP-Protocol-Version": "2026-07-28",
                            "Mcp-Method": method,
                        }
                        if method == "tools/call" and isinstance(
                            name := params.get("name"), str
                        ):
                            headers["Mcp-Name"] = name
                        params = {
                            **params,
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                "io.modelcontextprotocol/clientCapabilities": {},
                            },
                        }
                    result = await client.post(
                        "/" if root else "/workspace/mcp",
                        headers=headers,
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
        self.gateway_headers: list[httpx.Headers] = []
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
