"""A configured MCP transport must never fall back to native tool execution."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from galadril_mcp_core.client import MCPUnavailable, delegated_call
from galadril_scribe.mcp_transport import tool_request


class MCPTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_delegated_call_traces_the_failing_boundary(self) -> None:
        for reason, status, failure in (
            ("delegation_rejected", 403, None),
            (
                "provider_result",
                200,
                MCPUnavailable("private provider response"),
            ),
            ("provider_transport", 200, httpx.ConnectError("private endpoint")),
        ):
            with self.subTest(reason=reason):
                client = httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        Mock(
                            return_value=httpx.Response(
                                status,
                                json={"token": "a" * 64, "expires_in": 60},
                            )
                        )
                    )
                )
                with (
                    patch(
                        "galadril_mcp_core.client.trace.get_tracer"
                    ) as tracer,
                    patch(
                        "galadril_mcp_core.client._call", side_effect=failure
                    ),
                ):
                    span = tracer.return_value.start_as_current_span.return_value.__enter__.return_value
                    async with client:
                        with self.assertRaises(MCPUnavailable):
                            await delegated_call(
                                client,
                                "http://gateway/tools",
                                "parent",
                                capability_id="core.mcp.workspace",
                                endpoint="http://mcp/workspace/mcp",
                                resource="https://mcp.example/workspace/mcp",
                                tool="invoke_tool",
                                arguments={},
                            )
                    span.set_attribute.assert_any_call(
                        "mcp.failure.reason", reason
                    )
                    span.set_status.assert_called_once()

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
