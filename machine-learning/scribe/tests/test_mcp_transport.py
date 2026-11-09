"""A configured MCP transport must never fall back to native tool execution."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from galadril_mcp_core.client import MCPUnavailable
from galadril_scribe.mcp_transport import tool_request


class MCPTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_calls_use_mcp_and_keep_control_operations_local(
        self,
    ) -> None:
        async def gateway(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/internal/chat/tools")
            self.assertIn(b'"agent_state"', request.content)
            return httpx.Response(200, json={"revision": 1})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(gateway)
        ) as client:
            with patch(
                "galadril_scribe.mcp_transport.delegated_call",
                new_callable=AsyncMock,
            ) as call:
                call.return_value = {"value": [{"id": "account"}]}
                result = await tool_request(
                    client,
                    "http://gateway/internal/chat/tools",
                    "parent",
                    {
                        "operation": "email",
                        "request": {"operation": "accounts"},
                    },
                    endpoint="http://mcp/workspace/mcp",
                    resource="https://mcp.example/workspace/mcp",
                )
                self.assertEqual(result.json(), [{"id": "account"}])
                self.assertEqual(call.await_args.kwargs["tool"], "invoke_tool")
                self.assertEqual(
                    call.await_args.kwargs["capability_id"],
                    "core.mcp.workspace",
                )
                await tool_request(
                    client,
                    "http://gateway/internal/chat/tools",
                    "parent",
                    {"operation": "agent_state", "key": "checkpoint"},
                    endpoint="http://mcp/workspace/mcp",
                    resource="https://mcp.example/workspace/mcp",
                )
                self.assertEqual(call.await_count, 1)

    async def test_transport_failure_never_replays_a_mutation_via_native_http(
        self,
    ) -> None:
        async def forbidden(request: httpx.Request) -> httpx.Response:
            self.fail("MCP failure must not trigger a native fallback")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden)
        ) as client:
            with patch(
                "galadril_scribe.mcp_transport.delegated_call",
                new_callable=AsyncMock,
                side_effect=MCPUnavailable("unavailable"),
            ):
                response = await tool_request(
                    client,
                    "http://gateway/tools",
                    "parent",
                    {"operation": "compile_document", "request": {}},
                    endpoint="http://mcp/workspace/mcp",
                    resource="https://mcp.example/workspace/mcp",
                )
                self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
