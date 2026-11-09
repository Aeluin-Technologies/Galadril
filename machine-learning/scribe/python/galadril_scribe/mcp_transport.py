"""Workspace operations share MCP transport while execution control stays local."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from galadril_mcp_core.client import MCPUnavailable, delegated_call
from pydantic import JsonValue

if TYPE_CHECKING:
    from galadril_scribe.dependencies import Dependencies

_CONTROL = frozenset(
    {"agent_state", "conversation_memory", "mcp_delegate", "mcp_authorize"}
)


async def tool_request(
    http: httpx.AsyncClient,
    gateway: str,
    capability: str,
    payload: dict[str, JsonValue],
    *,
    endpoint: str | None,
    resource: str,
    timeout: float = 90,
) -> httpx.Response:
    """A failed MCP mutation must never be replayed through a second transport."""
    if endpoint is None or payload.get("operation") in _CONTROL:
        return await http.post(
            gateway,
            headers={"authorization": "Bearer " + capability},
            json=payload,
            timeout=timeout,
        )
    try:
        result = await delegated_call(
            http,
            gateway,
            capability,
            capability_id="core.mcp.workspace",
            resource=resource,
            endpoint=endpoint,
            tool="invoke_tool",
            arguments={"request": payload},
        )
        if "value" not in result:
            raise MCPUnavailable("Workspace result unconfirmed")
        return httpx.Response(
            200, json=result["value"], request=httpx.Request("POST", gateway)
        )
    except MCPUnavailable:
        return httpx.Response(
            503,
            json={"error": "Workspace MCP unavailable"},
            request=httpx.Request("POST", gateway),
        )


async def dependency_request(
    deps: Dependencies, payload: dict[str, JsonValue], *, timeout: float = 90
) -> httpx.Response:
    """UI adapters consume the same payloads without acquiring transport credentials."""
    return await tool_request(
        deps.client,
        deps.tools_url,
        deps.capability,
        payload,
        endpoint=deps.workspace_mcp_url,
        resource=deps.workspace_mcp_resource,
        timeout=timeout,
    )
