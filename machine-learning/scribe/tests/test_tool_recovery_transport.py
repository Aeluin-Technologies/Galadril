"""An offline MCP service can recover within the native agent's current run."""

from __future__ import annotations

import asyncio
import socket
import unittest
from collections.abc import AsyncIterator
from unittest.mock import patch

import httpx
import uvicorn
from galadril_pipeline.capabilities import Capability
from galadril_scribe.contracts import RunRequest, Settings
from galadril_scribe.dependencies import Dependencies
from galadril_scribe.extensions import ToolExtension
from galadril_scribe.runtime import Runtime
from mcp.server.fastmcp import Context, FastMCP
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from starlette.requests import Request


class RecoveryTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_mcp_error_reconnects_without_using_retry_budget(
        self,
    ) -> None:
        state = {"now": 0.0, "ready": False}
        effects: list[str | None] = []
        factories: list[bool] = []
        server = FastMCP(
            "optional-backend", stateless_http=True, json_response=True
        )

        @server.tool()
        async def ready(ctx: Context) -> str:
            incoming = ctx.request_context.request
            assert isinstance(incoming, Request)
            effects.append(incoming.headers.get("authorization"))
            if not state["ready"]:
                raise RuntimeError("private backend detail")
            return "Verified recovered service"

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.setblocking(False)
            port = listener.getsockname()[1]
            http = uvicorn.Server(
                uvicorn.Config(
                    server.streamable_http_app(),
                    log_level="critical",
                    access_log=False,
                )
            )
            task = asyncio.create_task(http.serve(sockets=[listener]))

            def factory() -> MCPToolset[Dependencies]:
                factories.append(True)
                return MCPToolset(
                    f"http://127.0.0.1:{port}/mcp", include_instructions=False
                )

            async def response(
                messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
                returned = [
                    part
                    for message in messages
                    if isinstance(message, ModelRequest)
                    for part in message.parts
                    if isinstance(part, ToolReturnPart)
                ]
                visible = {tool.name for tool in info.function_tools}
                if not returned:
                    yield {
                        0: DeltaToolCall(
                            name="optional_mcp_service_ready", json_args="{}"
                        )
                    }
                elif len(returned) == 1:
                    self.assertEqual(returned[0].outcome, "failed")
                    self.assertNotIn(
                        "private backend detail", str(returned[0].content)
                    )
                    self.assertNotIn("optional_mcp_service_ready", visible)
                    state.update(now=60.0, ready=True)
                    yield {
                        0: DeltaToolCall(
                            name="graph", json_args='{"entity_id":"entity"}'
                        )
                    }
                elif len(returned) == 2:
                    self.assertIn("optional_mcp_service_ready", visible)
                    yield {
                        0: DeltaToolCall(
                            name="optional_mcp_service_ready", json_args="{}"
                        )
                    }
                else:
                    yield "Verified recovered service"

            try:
                async with asyncio.timeout(3):
                    while not http.started:
                        await asyncio.sleep(0.01)
                configured = Settings(
                    gateway_tools_url="http://gateway/internal/chat/tools",
                    models={
                        "local": {
                            "model": "local",
                            "base_url": "http://model/v1",
                        }
                    },
                    default_model="local",
                )
                descriptor = Capability.model_validate(
                    {
                        "id": "optional.mcp.service",
                        "plugin_id": "optional",
                        "plugin_version": "1.0.0",
                        "kind": "mcp",
                        "targets": ["scribe"],
                        "name": "Optional service",
                        "description": "An optional backend.",
                        "parameters_schema": {"type": "object"},
                    }
                )
                with patch(
                    "galadril_scribe.extensions.monotonic",
                    side_effect=lambda: float(state["now"]),
                ):
                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(
                            lambda incoming: httpx.Response(200, json={})
                        )
                    ) as client:
                        runtime = Runtime(
                            configured,
                            client,
                            model=FunctionModel(stream_function=response),
                            extensions=(ToolExtension(descriptor, factory),),
                        )
                        chunks = [
                            chunk
                            async for chunk in runtime.stream(
                                RunRequest(
                                    prompt="Continue after the service returns",
                                    capability="a" * 64,
                                    tool_ids=(descriptor.id,),
                                )
                            )
                        ]
                self.assertEqual(chunks[-1].kind, "completed")
                self.assertEqual(effects, [None, None])
                self.assertEqual(len(factories), 2)
            finally:
                http.should_exit = True
                await asyncio.wait_for(task, 3)

    async def test_mcp_reconnects_after_service_starts_without_restarting_run(
        self,
    ) -> None:
        clock = {"now": 0.0}
        received_authority: list[str | None] = []
        server = FastMCP("recovering", stateless_http=True, json_response=True)

        @server.tool()
        async def ready(ctx: Context) -> str:
            incoming = ctx.request_context.request
            assert isinstance(incoming, Request)
            received_authority.append(incoming.headers.get("authorization"))
            return "Verified recovered service"

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.setblocking(False)
            port = listener.getsockname()[1]
            http = uvicorn.Server(
                uvicorn.Config(
                    server.streamable_http_app(),
                    log_level="critical",
                    access_log=False,
                )
            )
            task: asyncio.Task[None] | None = None

            async def response(
                messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
                nonlocal task
                returned = [
                    part
                    for message in messages
                    if isinstance(message, ModelRequest)
                    for part in message.parts
                    if isinstance(part, ToolReturnPart)
                ]
                visible = {tool.name for tool in info.function_tools}
                if not returned:
                    self.assertNotIn("external_0_ready", visible)
                    listener.listen()
                    task = asyncio.create_task(http.serve(sockets=[listener]))
                    async with asyncio.timeout(3):
                        while not http.started:
                            await asyncio.sleep(0.01)
                    clock["now"] = 60.0
                    yield {
                        0: DeltaToolCall(
                            name="graph", json_args='{"entity_id":"entity"}'
                        )
                    }
                elif len(returned) == 1:
                    self.assertIn("external_0_ready", visible)
                    yield {
                        0: DeltaToolCall(
                            name="external_0_ready", json_args="{}"
                        )
                    }
                else:
                    yield "Verified recovered service"

            configured = Settings(
                gateway_tools_url="http://gateway/internal/chat/tools",
                models={
                    "local": {"model": "local", "base_url": "http://model/v1"}
                },
                default_model="local",
                mcp_urls=[f"http://127.0.0.1:{port}/mcp"],
            )
            try:
                with patch(
                    "galadril_scribe.extensions.monotonic",
                    side_effect=lambda: clock["now"],
                ):
                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(
                            lambda incoming: httpx.Response(200, json={})
                        )
                    ) as client:
                        runtime = Runtime(
                            configured,
                            client,
                            model=FunctionModel(stream_function=response),
                        )
                        chunks = [
                            chunk
                            async for chunk in runtime.stream(
                                RunRequest(
                                    prompt="Use the optional service when it returns",
                                    capability="a" * 64,
                                )
                            )
                        ]
                self.assertEqual(chunks[-1].kind, "completed")
                self.assertEqual(received_authority, [None])
            finally:
                http.should_exit = True
                if task is not None:
                    await asyncio.wait_for(task, 3)


if __name__ == "__main__":
    unittest.main()
