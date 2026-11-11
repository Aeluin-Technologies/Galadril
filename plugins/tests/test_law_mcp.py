"""MCP authentication applies to discovery as well as tool execution."""

import json
import os
import ssl
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import httpx2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
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
    def __init__(
        self, token: str = "fixture-token", *, unified: bool = False
    ) -> None:
        self.token = token
        self.resource = (
            "https://mcp.example/" if unified else "https://mcp.example/law/mcp"
        )
        self.scopes = (
            ["workspace:use", "legal:read", "documents:read", "documents:write"]
            if unified
            else ["legal:read"]
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return (
            AccessToken(
                token=token,
                client_id="fixture",
                scopes=self.scopes,
                resource=self.resource,
            )
            if token == self.token
            else None
        )


class LawMCPTest(unittest.IsolatedAsyncioTestCase):
    async def test_librejustice_keeps_both_sides_and_the_full_research_evidence(
        self,
    ) -> None:
        evidence = {
            "url": "https://librejustice.fr/decision/ce/case-id",
            "text": "Vu la procédure. Considérant les motifs. DÉCIDE : rejet.",
            "case_chronology": [{"solution": "REJET"}],
        }
        official_id = "CETATEXT000025041089"
        passage = "Considérant les motifs."
        source = {
            "provider": "legifrance",
            "operation": "consult",
            "id": official_id,
            "data": {
                "text": {
                    "id": official_id,
                    "juridiction": "Conseil d'État",
                    "num": "123456",
                    "dateTexte": "2025-12-10",
                    "texte": evidence["text"],
                }
            },
        }
        template_id, version, project, job = (
            "a" * 32,
            "b" * 64,
            "c" * 32,
            "d" * 64,
        )
        document_ops: list[str] = []
        requests: list[httpx.Request] = []

        def libre(request: httpx.Request) -> httpx.Response:
            if request.url.host == "gateway.example":
                self.assertEqual(
                    request.headers["authorization"], "Bearer fixture-token"
                )
                payload = json.loads(request.content)
                self.assertEqual(payload["project_id"], project)
                args = payload["request"]["request"]
                operation = payload["request"]["operation"]
                document_ops.append(operation)
                if operation == "document_templates":
                    return httpx.Response(
                        200, json={"document_source_target": "requete.tex"}
                    )
                if operation == "compile_document":
                    self.assertEqual(args["template_id"], template_id)
                    self.assertEqual(args["version"], version)
                    self.assertIn(passage, args["document"]["source"])
                    return httpx.Response(
                        200, json={"job_id": job, "status": "queued"}
                    )
                self.assertEqual(operation, "document_job")
                self.assertEqual(args["job_id"], job)
                if args["action"] == "status":
                    return httpx.Response(
                        200, json={"job_id": job, "status": "completed"}
                    )
                self.assertEqual(args["action"], "save")
                return httpx.Response(
                    200, json={"file": {"id": "e" * 32, "name": "recours.pdf"}}
                )
            requests.append(request)
            self.assertEqual(request.url.host, "librejustice.fr")
            self.assertEqual(
                request.headers["Authorization"], "Bearer ljk_fixture"
            )
            if request.url.path == "/api/search":
                return httpx.Response(
                    200,
                    json={
                        "hits": [{"url": evidence["url"]}],
                        "facets": {"date_lecture_year": {"2026": 2}},
                        "total": 21,
                    },
                )
            if request.url.path == "/api/decision/case-id":
                return httpx.Response(200, json=evidence)
            if request.url.path == "/api/norm/BOI-BIC-PROV-50/130":
                return httpx.Response(200, json={"text": "Paragraphe fiscal"})
            self.assertEqual(request.url.path, "/api/norm/fr/code-civil/47")
            self.assertEqual(request.url.params["as_of"], "2020-02-29")
            return httpx.Response(200, json={"text": "Version historique"})

        with (
            patch.dict(
                os.environ,
                {
                    "LIBREJUSTICE_API_KEY": "ljk_fixture",
                    "LIBREJUSTICE_API_KEY_FILE": "",
                    "MCP_WORKSPACE_GATEWAY_URL": "https://gateway.example/tools",
                    "MCP_DOWNLOAD_ORIGIN": "https://galadril.example",
                },
            ),
            patch("galadril_mcp_service.providers.piste.PisteCredentials.load"),
            patch(
                "galadril_mcp_service.providers.piste.PisteClient.decision",
                return_value=source,
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(libre)
            ) as outgoing:
                server = create_server(
                    MCPAuth(
                        issuer="https://auth.example/",
                        resource="https://mcp.example/",
                        jwks_url="https://auth.example/jwks",
                        scopes=("workspace:use",),
                    ),
                    outgoing,
                    provider="unified",
                    verifier=_Verifier(unified=True),
                )
                app = server.namespace_app()
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app),
                        base_url="https://mcp.example",
                        headers={
                            "authorization": "Bearer fixture-token",
                            "accept": "application/json, text/event-stream",
                        },
                    ) as client,
                ):

                    async def call(
                        name: str, arguments: dict[str, object]
                    ) -> dict:
                        response = await client.post(
                            "/",
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {
                                    "name": name,
                                    "arguments": arguments,
                                },
                            },
                        )
                        return response.json()["result"]

                    for outcomes in (
                        ["ANNULATION", "SATISFACTION_PARTIELLE"],
                        ["REJET"],
                    ):
                        result = await call(
                            "search_decisions",
                            {
                                "request": {
                                    "query": "vice de procédure",
                                    "seat": ["FR.CE"],
                                    "solution": outcomes,
                                    "sort": "date_desc",
                                    "date_from": "2026-01-01",
                                },
                            },
                        )
                        data = result["structuredContent"]
                        self.assertEqual(data["total"], 21)
                        self.assertEqual(
                            data["facets"]["date_lecture_year"]["2026"], 2
                        )
                        self.assertEqual(
                            requests[-1].url.params.get_list("solution"),
                            outcomes,
                        )
                        self.assertEqual(
                            requests[-1].url.params["seat"], "FR.CE"
                        )
                        self.assertEqual(
                            requests[-1].url.params["sort"], "date_desc"
                        )
                    result = await call(
                        "get_decision", {"url": evidence["url"]}
                    )
                    self.assertEqual(
                        result["structuredContent"]["case_chronology"],
                        evidence["case_chronology"],
                    )
                    self.assertEqual(
                        result["structuredContent"]["text"], evidence["text"]
                    )
                    result = await call(
                        "get_norm",
                        {
                            "request": {
                                "url": "https://librejustice.fr/norm/fr/code-civil/47",
                                "as_of": "2020-02-29",
                            }
                        },
                    )
                    self.assertEqual(
                        result["structuredContent"]["text"],
                        "Version historique",
                    )
                    result = await call(
                        "get_norm",
                        {
                            "request": {
                                "text_id": "BOI-BIC-PROV-50",
                                "article": "130",
                            }
                        },
                    )
                    self.assertEqual(
                        result["structuredContent"]["text"], "Paragraphe fiscal"
                    )
                    result = await call(
                        "verify_citation",
                        {
                            "citation": {
                                "provider": "legifrance",
                                "identifier": official_id,
                                "court": "Conseil d'État",
                                "number": "123456",
                                "date": "2025-12-10",
                                "passage": passage,
                            }
                        },
                    )
                    verification = result["structuredContent"]
                    self.assertTrue(verification["identity_verified"])
                    self.assertTrue(verification["passage_verified"])
                    self.assertFalse(verification["applicability_verified"])
                    result = await call(
                        "document_templates",
                        {
                            "project_id": project,
                            "request": {
                                "action": "read",
                                "template_id": template_id,
                                "version": version,
                            },
                        },
                    )
                    self.assertEqual(
                        result["structuredContent"]["document_source_target"],
                        "requete.tex",
                    )
                    result = await call(
                        "compile_document",
                        {
                            "project_id": project,
                            "request": {
                                "template_id": template_id,
                                "version": version,
                                "idempotency_key": "265953a8-690d-4d19-9816-7097cfd320e6",
                                "document": {"source": passage},
                            },
                        },
                    )
                    self.assertEqual(result["structuredContent"]["job_id"], job)
                    result = await call(
                        "document_job_status",
                        {"project_id": project, "job_id": job},
                    )
                    self.assertEqual(
                        result["structuredContent"]["status"], "completed"
                    )
                    result = await call(
                        "document_job",
                        {
                            "project_id": project,
                            "request": {
                                "action": "save",
                                "job_id": job,
                                "name": "recours.pdf",
                            },
                        },
                    )
                    self.assertIn(
                        "resource_link",
                        {block["type"] for block in result["content"]},
                    )
                    self.assertEqual(
                        document_ops,
                        [
                            "document_templates",
                            "compile_document",
                            "document_job",
                            "document_job",
                        ],
                    )
                    before = len(requests)
                    for url in (
                        "https://other.example/decision/ce/a",
                        "https://librejustice.fr/decision/ce/%2e%2e",
                    ):
                        result = await call("get_decision", {"url": url})
                        self.assertTrue(result["isError"])
                    self.assertEqual(len(requests), before)

    async def test_federated_search_runs_both_providers_and_deduplicates(
        self,
    ) -> None:
        import asyncio

        official_started = asyncio.Event()
        libre_started = asyncio.Event()
        overlap = "CETATEXT000025041089"

        async def piste(*args: object, **kwargs: object) -> dict[str, object]:
            official_started.set()
            await asyncio.wait_for(libre_started.wait(), 2)
            return {
                "provider": "legifrance",
                "data": {
                    "results": [
                        {"id": overlap},
                        *({"id": f"CETATEXT{i:012d}"} for i in range(1, 9)),
                    ]
                },
            }

        async def libre(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "librejustice.fr")
            self.assertEqual(request.url.path, "/api/search")
            self.assertEqual(request.url.params.get("ai_rerank"), "true")
            self.assertEqual(
                request.headers["Authorization"], "Bearer ljk_fixture"
            )
            libre_started.set()
            await asyncio.wait_for(official_started.wait(), 2)
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "title": "Duplicate",
                            "url": "/decision/ce/a",
                            "official_url": f"https://www.legifrance.gouv.fr/ceta/id/{overlap}",
                        },
                        *(
                            {"title": str(i), "url": f"/decision/ce/{i}"}
                            for i in range(1, 9)
                        ),
                    ]
                },
            )

        with (
            tempfile.TemporaryDirectory() as secrets,
            patch.dict(
                os.environ,
                {
                    "LIBREJUSTICE_API_KEY_FILE": str(
                        Path(secrets) / "librejustice-key"
                    ),
                    "LIBREJUSTICE_API_KEY": "",
                    "MCP_CREDENTIALS_DIR": secrets,
                },
            ),
            patch("galadril_mcp_service.providers.piste.PisteCredentials.load"),
            patch(
                "galadril_mcp_service.providers.piste.PisteClient.search",
                side_effect=piste,
            ),
        ):
            credential = Path(secrets) / "librejustice-key"
            credential.write_text("ljk_fixture\n")
            credential.chmod(0o600)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(libre)
            ) as outgoing:
                server = create_server(
                    MCPAuth(
                        issuer="https://auth.example/",
                        resource="https://mcp.example/law/mcp",
                        jwks_url="https://auth.example/jwks",
                        scopes=("legal:read",),
                    ),
                    outgoing,
                    provider="piste",
                    verifier=_Verifier(),
                )
                app = server.namespace_app()
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app),
                        base_url="https://mcp.example",
                        headers={
                            "authorization": "Bearer fixture-token",
                            "accept": "application/json, text/event-stream",
                        },
                    ) as client,
                ):
                    response = await client.post(
                        "/law/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "search",
                                "arguments": {"query": "vice de procédure"},
                            },
                        },
                    )
        result = response.json()["result"]["structuredContent"]
        self.assertEqual(len(result["data"]["results"]), 5)
        self.assertEqual(len(result["librejustice"]["hits"]), 5)
        self.assertEqual(result["librejustice"]["hits"][0]["title"], "1")
        self.assertEqual(result["librejustice"]["duplicates"], [overlap])
        self.assertFalse(result["citation_verified"])

    async def test_citation_verification_consults_only_legifrance(self) -> None:
        identifier = "CETATEXT000025041089"
        citation = {
            "provider": "judilibre",
            "identifier": identifier,
            "number": "335033",
            "court": "Conseil d'État",
            "date": "2011-12-23",
            "passage": "garantie",
        }
        record = {
            "operation": "consult",
            "provider": "legifrance",
            "id": identifier,
            "data": {
                "text": {
                    "id": identifier,
                    "num": "335033",
                    "juridiction": "Conseil d'État",
                    "dateTexte": "2011-12-23",
                    "texte": "Texte de la garantie.",
                }
            },
        }
        with (
            patch("galadril_mcp_service.providers.piste.PisteCredentials.load"),
            patch(
                "galadril_mcp_service.providers.piste.PisteClient.decision",
                return_value=record,
            ) as decision,
        ):
            async with httpx.AsyncClient() as outgoing:
                server = create_server(
                    MCPAuth(
                        issuer="https://auth.example/",
                        resource="https://mcp.example/law/mcp",
                        jwks_url="https://auth.example/jwks",
                        scopes=("legal:read",),
                    ),
                    outgoing,
                    provider="piste",
                    verifier=_Verifier(),
                )
                app = server.namespace_app()
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app),
                        base_url="https://mcp.example",
                        headers={
                            "authorization": "Bearer fixture-token",
                            "accept": "application/json, text/event-stream",
                        },
                    ) as client,
                ):
                    for provider in ("judilibre", "legifrance"):
                        citation["provider"] = provider
                        response = await client.post(
                            "/law/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": provider,
                                "method": "tools/call",
                                "params": {
                                    "name": "verify_citation",
                                    "arguments": {"citation": citation},
                                },
                            },
                        )
                        result = response.json()["result"]["structuredContent"]
                        if provider == "judilibre":
                            self.assertEqual(result["status"], "unverified")
                            decision.assert_not_awaited()
                        else:
                            self.assertTrue(result["passage_verified"])
                            decision.assert_awaited_once_with(
                                provider, identifier
                            )

    async def test_disabled_legal_provider_is_a_normal_result_and_can_be_enabled_later(
        self,
    ) -> None:
        from galadril_mcp_service.providers.piste_api import (
            PisteCredentials,
            PisteUnavailable,
        )

        async def reject_network(request: httpx.Request) -> httpx.Response:
            self.fail(
                "An unconfigured legal provider must not call external services"
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(reject_network)
        ) as outgoing:
            server = create_server(
                MCPAuth(
                    issuer="https://auth.example/",
                    resource="https://mcp.example/law/mcp",
                    jwks_url="https://auth.example/jwks",
                    scopes=("legal:read",),
                ),
                outgoing,
                provider="piste",
                verifier=_Verifier(),
            )
            app = server.namespace_app()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="https://mcp.example",
                    headers={
                        "authorization": "Bearer fixture-token",
                        "accept": "application/json, text/event-stream",
                    },
                ) as http:
                    with patch(
                        "galadril_mcp_service.providers.piste.PisteCredentials.load",
                        side_effect=PisteUnavailable(
                            "PRIVATE_CONFIGURATION_DETAIL"
                        ),
                    ):
                        for name, arguments in (
                            ("search", {"query": "vice de procédure"}),
                            ("fetch", {"identifier": "CETATEXT000025041089"}),
                            (
                                "verify_citation",
                                {
                                    "citation": {
                                        "provider": "legifrance",
                                        "identifier": "CETATEXT000025041089",
                                        "number": "335033",
                                        "court": "Conseil d'État",
                                        "date": "2011-12-23",
                                        "passage": "garantie",
                                    }
                                },
                            ),
                        ):
                            response = await http.post(
                                "/law/mcp",
                                json={
                                    "jsonrpc": "2.0",
                                    "id": name,
                                    "method": "tools/call",
                                    "params": {
                                        "name": name,
                                        "arguments": arguments,
                                    },
                                },
                            )
                            self.assertEqual(response.status_code, 200)
                            result = response.json()["result"]
                            self.assertFalse(result.get("isError", False))
                            self.assertEqual(
                                result["structuredContent"]["status"],
                                "unavailable",
                            )
                            self.assertNotIn(
                                "PRIVATE_CONFIGURATION_DETAIL", response.text
                            )
                    with (
                        patch(
                            "galadril_mcp_service.providers.piste.PisteCredentials.load",
                            return_value=PisteCredentials(
                                client_id="fixture",
                                client_secret="fixture-secret",
                            ),
                        ),
                        patch(
                            "galadril_mcp_service.providers.piste.PisteClient.search",
                            new_callable=AsyncMock,
                            return_value={"items": []},
                        ) as search,
                    ):
                        response = await http.post(
                            "/law/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": "enabled",
                                "method": "tools/call",
                                "params": {
                                    "name": "search",
                                    "arguments": {"query": "vice de procédure"},
                                },
                            },
                        )
                        self.assertFalse(
                            response.json()["result"].get("isError", False)
                        )
                        self.assertEqual(
                            {
                                "items": response.json()["result"][
                                    "structuredContent"
                                ]["items"]
                            },
                            {"items": []},
                        )
                        search.assert_awaited_once()
                    for tool, arguments in (
                        ("search", {"query": "vice de procédure"}),
                        ("fetch", {"identifier": "fixture-decision"}),
                    ):
                        response = await http.post(
                            "/law/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": tool,
                                "method": "tools/call",
                                "params": {
                                    "name": tool,
                                    "arguments": {
                                        "provider": "judilibre",
                                        **arguments,
                                    },
                                },
                            },
                        )
                        result = response.json()["result"]
                        self.assertFalse(result.get("isError", False))
                        self.assertEqual(
                            result["structuredContent"]["status"], "unavailable"
                        )

    def test_internal_jwks_ca_preserves_certificate_and_hostname_verification(
        self,
    ) -> None:
        from galadril_mcp_service.server import client_tls_context

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name(
            [
                x509.NameAttribute(
                    x509.NameOID.COMMON_NAME, "Fixture MCP internal authority"
                )
            ]
        )
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=0), critical=True
            )
            .sign(key, hashes.SHA256())
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ca.pem"
            path.write_bytes(
                certificate.public_bytes(serialization.Encoding.PEM)
            )
            with patch.dict(os.environ, {"MCP_TLS_CA_FILE": str(path)}):
                context = client_tls_context()
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertIn(
                "Fixture MCP internal authority", str(context.get_ca_certs())
            )
            path.write_text("invalid authority")
            with (
                patch.dict(os.environ, {"MCP_TLS_CA_FILE": str(path)}),
                self.assertRaises(ssl.SSLError),
            ):
                client_tls_context()

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
            CallToolResult(content=[], is_error=True),
            CallToolResult(content=[]),
            CallToolResult(content=[text("{}"), text("{}")]),
            CallToolResult(content=[text("x" * 262145)]),
            CallToolResult(content=[text("[]")]),
            CallToolResult(
                content=[], structured_content={"value": "x" * 262144}
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
                        metadata = await http.get(
                            "/.well-known/oauth-protected-resource"
                        )
                        assertions.assertEqual(metadata.status_code, 200)
                        assertions.assertEqual(
                            metadata.json()["resource"], "https://mcp.example/"
                        )
                        assertions.assertLessEqual(
                            {"legal:read", "email:read", "email:write"},
                            set(metadata.json()["scopes_supported"]),
                        )
                        root = await http.post(
                            "/", json={"method": "tools/list"}
                        )
                        assertions.assertEqual(root.status_code, 401)
                        assertions.assertIn(
                            'resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"',
                            root.headers["www-authenticate"],
                        )
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
        namespaces.append(
            {
                "provider": "unified",
                "capability_id": "fixture.workspace",
                "path": "/",
                "scopes": ["workspace:use"],
            }
        )
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

            app = server.namespace_app()

            mcp_client = httpx2.AsyncClient

            def transport(
                *, headers: dict[str, str], timeout: float, trust_env: bool
            ) -> httpx2.AsyncClient:
                self.assertEqual(headers["authorization"], "Bearer " + child)
                return mcp_client(
                    transport=httpx2.ASGITransport(app),
                    headers=headers,
                    timeout=timeout,
                    trust_env=trust_env,
                )

            async with server.session_manager.run():
                with patch(
                    "galadril_mcp_core.client.httpx2.AsyncClient",
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
            app = server.namespace_app()
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
                    {
                        "search",
                        "fetch",
                        "verify_citation",
                        "search_decisions",
                        "get_decision",
                        "search_norms",
                        "get_norm",
                    },
                )
            names = {tool.name for tool in await server.list_tools()}
            self.assertEqual(
                names,
                {
                    "search",
                    "fetch",
                    "verify_citation",
                    "search_decisions",
                    "get_decision",
                    "search_norms",
                    "get_norm",
                },
            )


if __name__ == "__main__":
    unittest.main()
