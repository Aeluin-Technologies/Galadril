"""Exchange generation authority for a short-lived, single-resource MCP grant."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr


class MCPUnavailable(RuntimeError):
    """An MCP call failed without confirming any result."""


class _Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: SecretStr = Field(min_length=64, max_length=64)
    expires_in: int = Field(ge=1, le=90)


def _public_result(result: CallToolResult) -> dict[str, JsonValue]:
    """Accept bounded structured output without treating tool errors as evidence."""
    if result.is_error:
        raise MCPUnavailable("MCP provider did not confirm a result")
    value = result.structured_content
    if value is None:
        texts = [item.text for item in result.content if item.type == "text"]
        if len(texts) != 1 or len(texts[0]) > 262144:
            raise MCPUnavailable("MCP result exceeds limit")
        value = json.loads(texts[0])
    if not isinstance(value, dict) or len(json.dumps(value)) > 262144:
        raise MCPUnavailable("MCP result is invalid or exceeds limit")
    return value


async def _call(
    grant: _Grant,
    resource: str,
    endpoint: str,
    tool: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Keep the attenuated token inside the transport, outside tool arguments."""
    async with httpx2.AsyncClient(
        headers={
            "authorization": "Bearer " + grant.token.get_secret_value(),
            "host": urlsplit(resource).netloc,
        },
        timeout=40,
        trust_env=False,
    ) as transport:
        async with Client(
            streamable_http_client(
                endpoint, http_client=transport, terminate_on_close=False
            ),
            read_timeout_seconds=40,
        ) as client:
            metadata: dict[str, str] = {}
            TraceContextTextMapPropagator().inject(metadata)
            result = await client.call_tool(
                tool, arguments, meta=metadata or None
            )
    return _public_result(result)


async def delegated_call(
    http: httpx.AsyncClient,
    gateway: str,
    generation_capability: str,
    *,
    capability_id: str,
    resource: str,
    endpoint: str,
    tool: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Exchange authority with Gateway before invoking a registered MCP resource.

    Args:
        http: Internal client used only for Gateway delegation.
        gateway: Operator-configured Gateway tool endpoint.
        generation_capability: Parent authority, never forwarded to the MCP.
        capability_id: Registered capability selected for this generation.
        resource: Exact OAuth resource from the trusted capability binding.
        endpoint: Operator-configured route to that resource.
        tool: Registered remote tool name.
        arguments: Public tool arguments, excluding transport credentials.

    Returns:
        Confirmed, bounded tool output.

    Raises:
        MCPUnavailable: Delegation or MCP execution did not confirm a result.
    """
    with trace.get_tracer("galadril.mcp").start_as_current_span(
        "mcp.client.call",
        attributes={"mcp.id": capability_id, "mcp.tool": tool},
        record_exception=False,
        set_status_on_exception=False,
    ):
        try:
            response = await http.post(
                gateway,
                headers={"authorization": "Bearer " + generation_capability},
                json={
                    "operation": "mcp_delegate",
                    "capability_id": capability_id,
                    "resource": resource,
                },
                timeout=15,
            )
            if response.status_code != 200 or len(response.content) > 4096:
                raise MCPUnavailable("MCP delegation unavailable")
            grant = _Grant.model_validate_json(response.content)
            return await _call(grant, resource, endpoint, tool, arguments)
        except (
            httpx.HTTPError,
            httpx2.HTTPError,
            ValueError,
            MCPError,
            ExceptionGroup,
        ):
            raise MCPUnavailable("MCP transport unavailable") from None
