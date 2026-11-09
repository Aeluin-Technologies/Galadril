"""An unavailable legal MCP cannot terminate the native agent's tool loop."""

import json
import unittest
from collections.abc import AsyncIterator

import httpx
from galadril_pipeline.capabilities import Capability
from galadril_plugins import PluginRegistry
from galadril_plugins.law.piste_tools import research_tools
from galadril_scribe.contracts import RunRequest, Settings
from galadril_scribe.extensions import ToolExtension
from galadril_scribe.runtime import Runtime
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel


class LegalAvailabilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_mcp_allows_web_fallback_and_a_completed_response(
        self,
    ) -> None:
        calls: list[str] = []

        def gateway(request: httpx.Request) -> httpx.Response:
            operation = json.loads(request.content)["operation"]
            calls.append(operation)
            if operation == "mcp_delegate":
                return httpx.Response(503)
            return httpx.Response(
                200, json={"content": "Public legal source", "sources": []}
            )

        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            returned = [
                part
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            if not returned:
                name = next(
                    tool.name
                    for tool in info.function_tools
                    if tool.name.endswith("search_official_law")
                )
                yield {
                    0: DeltaToolCall(
                        name=name,
                        json_args='{"provider":"legifrance","query":"vice de procédure"}',
                    )
                }
            elif len(returned) == 1:
                self.assertIn("unavailable", str(returned[0].content))
                yield {
                    0: DeltaToolCall(
                        name="web_search",
                        json_args='{"query":"vice de procédure"}',
                    )
                }
            else:
                self.assertIn("Public legal source", str(returned[-1].content))
                yield "The legal API is unavailable. I continued using authorized public web research."

        descriptor = Capability.model_validate(
            {
                "id": "fixture.mcp.legal",
                "plugin_id": "fixture",
                "plugin_version": "1.0.0",
                "kind": "mcp",
                "targets": ["scribe"],
                "name": "Official legal research",
                "description": "Optional legal provider",
                "parameters_schema": {"type": "object"},
            }
        )
        settings = Settings(
            gateway_tools_url="http://gateway/internal/chat/tools",
            models={"local": {"model": "local", "base_url": "http://model/v1"}},
            default_model="local",
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(gateway)
        ) as client:
            runtime = Runtime(
                settings,
                client,
                model=FunctionModel(stream_function=respond),
                plugins=PluginRegistry.from_manifests(()),
                extensions=(ToolExtension(descriptor, research_tools),),
            )
            chunks = [
                chunk
                async for chunk in runtime.stream(
                    RunRequest(
                        prompt="Research the issue and explain missing sources",
                        capability="a" * 64,
                        tool_ids=(descriptor.id,),
                        web_search=True,
                    )
                )
            ]
        self.assertEqual(chunks[-1].kind, "completed")
        self.assertIn("mcp_delegate", calls)
        self.assertIn("web", calls)
        self.assertIn("continued", "".join(chunk.content for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
