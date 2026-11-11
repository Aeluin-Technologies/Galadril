"""Expose the workspace and legal catalogs under one authenticated audience."""

import httpx
from mcp.server.mcpserver import MCPServer

from galadril_mcp_service.providers import piste, workspace

SCOPES = (
    "workspace:use",
    "legal:read",
    "email:read",
    "email:write",
    "documents:read",
    "documents:write",
    "data:read",
)


def register(server: MCPServer[None], http: httpx.AsyncClient) -> None:
    """Keep each tool's authorization checks even though discovery is shared."""
    workspace.register(server, http, unified=True)
    piste.register(server, http, unified=True)
