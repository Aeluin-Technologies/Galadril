"""Expose workspace operations without transferring authorization to the model."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ResourceLink, TextContent, ToolAnnotations
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)
from pydantic import Field, JsonValue

from galadril_mcp_service.authorization import ToolScopeRequired
from galadril_mcp_service.providers.workspace_contracts import (
    Compilation,
    Identifier,
    JobRequest,
    MailRead,
    Revision,
    SaveDraft,
    TemplatePut,
    TemplateQuery,
    TemplateWrite,
)

FileIds = Annotated[tuple[Identifier, ...], Field(max_length=16)]
_JOB_VIEW_URI = "ui://galadril/document-job.html"


def document_apps() -> Apps:
    """Serve a network-free view over the already-authorized job response."""
    apps = Apps()
    apps.add_html_resource(
        _JOB_VIEW_URI,
        Path(__file__)
        .with_name("document_job.html")
        .read_text(encoding="utf-8"),
        name="Document compilation",
        csp=ResourceCsp(connect_domains=[], resource_domains=[]),
    )
    return apps


def _result(value: dict[str, JsonValue]) -> CallToolResult:
    """Published files remain behind live Galadril authorization, never raw S3 paths."""
    content: list[TextContent | ResourceLink] = [
        TextContent(type="text", text=json.dumps(value, ensure_ascii=False))
    ]
    file = value.get("file")
    if isinstance(file, dict):
        identifier, name = file.get("id"), file.get("name")
        origin = os.environ["MCP_DOWNLOAD_ORIGIN"].rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ToolError("Document download unavailable")
        if isinstance(identifier, str) and re.fullmatch(
            r"[a-f0-9]{32}", identifier
        ):
            url = origin + "/pengolo/files/" + identifier
            content.extend(
                [
                    ResourceLink(
                        type="resource_link",
                        uri=url,
                        name=str(name or "document.pdf"),
                        mime_type=str(file.get("mime") or "application/pdf"),
                    ),
                    TextContent(
                        type="text",
                        text="Download (Galadril sign-in required): " + url,
                    ),
                ]
            )
    return CallToolResult(content=content, structured_content=value)


def register(
    server: MCPServer[None], http: httpx.AsyncClient, *, unified: bool = False
) -> None:
    """Keep transport identity separate from arguments accepted from any MCP client."""

    email_read = "email:read" if unified else "mail:read"
    draft_write = "email:write" if unified else "mail:draft"
    send_write = "email:write" if unified else "mail:send"

    async def invoke(
        scope: str,
        operation: str,
        arguments: dict[str, JsonValue],
        project_id: str | None = None,
        *,
        envelope: bool = False,
    ) -> CallToolResult:
        identity = get_access_token()
        if identity is None:
            raise ToolError("Workspace scope unavailable")
        if scope not in identity.scopes:
            raise ToolScopeRequired(scope)
        if (
            project_id is not None
            and re.fullmatch(r"[a-f0-9]{32}", project_id) is None
        ):
            raise ToolError("Invalid project")
        # The SDK extracts the parent. Propagate trace context only to Gateway.
        propagator = TraceContextTextMapPropagator()
        with trace.get_tracer("galadril.mcp").start_as_current_span(
            "mcp.workspace.call",
            attributes={"mcp.operation": operation},
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                endpoint = (
                    "MCP_WORKSPACE_DELEGATED_URL"
                    if identity.client_id == "galadril-pengolo"
                    else "MCP_WORKSPACE_GATEWAY_URL"
                )
                headers = {"authorization": "Bearer " + identity.token}
                propagator.inject(headers)
                response = await http.post(
                    os.environ[endpoint],
                    headers=headers,
                    json={
                        "project_id": project_id,
                        "request": {"operation": operation, **arguments},
                    },
                    timeout=90,
                    follow_redirects=False,
                )
                if (
                    response.status_code != 200
                    or len(response.content) > 262144
                ):
                    raise ToolError("Workspace operation unavailable")
                value: JsonValue = response.json()
                if envelope:
                    return _result({"value": value})
                if not isinstance(value, dict):
                    value = {"result": value}
                return _result(value)
            except (httpx.HTTPError, json.JSONDecodeError):
                raise ToolError("Workspace transport unavailable") from None

    def annotations(
        read: bool, destructive: bool = False, external: bool = False
    ) -> ToolAnnotations:
        return ToolAnnotations(
            read_only_hint=read,
            destructive_hint=destructive,
            idempotent_hint=read,
            open_world_hint=external,
        )

    def auth(scope: str) -> dict[str, JsonValue]:
        return {
            "securitySchemes": [
                {"type": "oauth2", "scopes": ["workspace:use", scope]}
            ]
        }

    @server.tool(annotations=annotations(True), meta=auth(email_read))
    async def read_email(request: MailRead) -> CallToolResult:
        """Read accessible mail. Start with accounts, then use contacts to identify recipients and messages with query to find earlier correspondence. Open the matching thread or message by ID before drafting a reply. This tool cannot send mail."""
        return await invoke(
            email_read,
            "email",
            {"request": request.model_dump(mode="json", exclude_unset=True)},
        )

    @server.tool(annotations=annotations(False), meta=auth(draft_write))
    async def save_email_draft(
        request: SaveDraft, file_ids: FileIds = ()
    ) -> CallToolResult:
        """Save a reviewable draft; never approve or send it. Attach generated files by file_ids with documents:read scope: bytes stay inside Galadril. Existing drafts require their current revision. Returned attachments are metadata only; retain their file_ids when revising."""
        payload = request.model_dump(mode="json", exclude_none=True)
        return await invoke(
            draft_write,
            "email_save_draft",
            {"request": payload, "file_ids": list(file_ids)},
        )

    @server.tool(
        annotations=annotations(False, True, True), meta=auth(send_write)
    )
    async def send_email(
        account: Identifier,
        draft: Identifier,
        revision: Annotated[int, Field(ge=1)],
    ) -> CallToolResult:
        """Send an exact saved draft revision. Show the sender, recipients, content and attachments before asking for confirmation. Call only after user approval. If the account requires review in Galadril, return the review link and report the draft as unsent."""
        return await invoke(
            send_write,
            "email_send",
            {"account": account, "draft": draft, "revision": revision},
        )

    @server.tool(annotations=annotations(True), meta=auth("data:read"))
    async def search_data(question: str, limit: int = 10) -> CallToolResult:
        """Search only indexed data the authenticated Galadril user may read."""
        return await invoke(
            "data:read", "search", {"question": question, "limit": limit}
        )

    @server.tool(annotations=annotations(True), meta=auth("documents:read"))
    async def document_templates(
        request: TemplateQuery, project_id: Identifier | None = None
    ) -> CallToolResult:
        """List/read authorized LaTeX templates and their assets. Read CLS and TeX sources before writing. Use plugin_catalog to discover installed templates. Private document fields are bound by the compiler and never returned."""
        return await invoke(
            "documents:read",
            "document_templates",
            {"request": request.model_dump(mode="json", exclude_none=True)},
            project_id,
        )

    @server.tool(annotations=annotations(True), meta=auth("documents:write"))
    async def document_case_status(
        template_id: Identifier, project_id: Identifier
    ) -> CallToolResult:
        """Check project and vault readiness. Returns missing field names and profile_ready without reading out private values or editing the project."""
        return await invoke(
            "documents:write",
            "document_templates",
            {
                "request": {
                    "action": "document_case",
                    "template_id": template_id,
                }
            },
            project_id,
        )

    @server.tool(
        annotations=annotations(False, True), meta=auth("documents:write")
    )
    async def manage_document_templates(
        request: TemplateWrite, project_id: Identifier | None = None
    ) -> CallToolResult:
        """Create, publish, delete or export an authorized LaTeX template, or update public case fields. Plugin enablement and private profiles remain user settings, outside model authority."""
        payload = request.model_dump(mode="json", exclude_none=True)
        if isinstance(request, TemplatePut):
            payload["template"] = request.template.wire()
        return await invoke(
            "documents:write",
            "document_templates",
            {"request": payload},
            project_id,
        )

    @server.tool(annotations=annotations(False), meta=auth("documents:write"))
    async def compile_document(
        request: Compilation, project_id: Identifier | None = None
    ) -> CallToolResult:
        """Generate the requested PDF in Galadril without sending or filing it. Pass the requested LaTeX in document.source using the document_source_target returned by document_templates. The compiler binds vault fields automatically. Poll the returned job_id with document_job_status."""
        return await invoke(
            "documents:write",
            "compile_document",
            {"request": request.model_dump(mode="json")},
            project_id,
        )

    @server.tool(
        annotations=annotations(True),
        meta={**auth("documents:write"), "ui": {"resourceUri": _JOB_VIEW_URI}},
    )
    async def document_job_status(
        job_id: Revision, project_id: Identifier | None = None
    ) -> CallToolResult:
        """Read compilation state in the original project. When completed, use document_job with action save to obtain the PDF link."""
        return await invoke(
            "documents:write",
            "document_job",
            {"request": {"action": "status", "job_id": job_id}},
            project_id,
        )

    @server.tool(annotations=annotations(False), meta=auth("documents:write"))
    async def document_job(
        request: JobRequest, project_id: Identifier | None = None
    ) -> CallToolResult:
        """Poll, cancel or save a compilation job in this user/project scope. Saved PDFs return both an authorized resource link and a textual download URL."""
        return await invoke(
            "documents:write",
            "document_job",
            {"request": request.model_dump(mode="json")},
            project_id,
        )

    @server.tool(annotations=annotations(True), meta=auth("documents:read"))
    async def request_document_signature(file_id: Identifier) -> CallToolResult:
        """Return the Galadril page where the user can review and sign an accessible PDF."""
        return await invoke(
            "documents:read", "request_document_signature", {"file_id": file_id}
        )

    @server.tool(annotations=annotations(False, True, True))
    async def invoke_tool(request: dict[str, JsonValue]) -> CallToolResult:
        """Internal Pengolo transport for generation-bound tools; unavailable to external OAuth callers."""
        identity = get_access_token()
        if identity is None or identity.client_id != "galadril-pengolo":
            raise ToolError("Generation-bound tool unavailable")
        operation = request.get("operation")
        if not isinstance(operation, str):
            raise ToolError("Invalid tool operation")
        arguments = {
            key: value for key, value in request.items() if key != "operation"
        }
        return await invoke(
            "workspace:use", operation, arguments, envelope=True
        )
