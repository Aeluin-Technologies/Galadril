"""Unavailable optional dependencies must not terminate a useful conversation."""

from __future__ import annotations

import json
import unittest
from collections.abc import AsyncIterator
from typing import Self
from unittest.mock import patch

import httpx
from galadril_pipeline.capabilities import Capability
from galadril_scribe.contracts import RunRequest, Settings
from galadril_scribe.dependencies import Dependencies
from galadril_scribe.extensions import ToolExtension
from galadril_scribe.runtime import Runtime
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from structlog.testing import capture_logs


def settings() -> Settings:
    return Settings(
        gateway_tools_url="http://gateway/internal/chat/tools",
        models={"local": {"model": "local", "base_url": "http://model/v1"}},
        default_model="local",
    )


def descriptor() -> Capability:
    return Capability.model_validate(
        {
            "id": "optional.algorithm.service",
            "plugin_id": "optional",
            "plugin_version": "1.0.0",
            "kind": "algorithm",
            "targets": ["scribe"],
            "name": "Optional service",
            "description": "An optional deployment dependency.",
            "parameters_schema": {"type": "object"},
        }
    )


class ToolResilienceTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_tool_can_return_after_cooldown_without_replaying_failure(
        self,
    ) -> None:
        clock = {"now": 0.0}
        calls: list[str] = []

        def gateway(incoming: httpx.Request) -> httpx.Response:
            operation = json.loads(incoming.content)["operation"]
            calls.append(operation)
            if (
                operation == "document_templates"
                and calls.count(operation) == 1
            ):
                return httpx.Response(503)
            return httpx.Response(200, json={"templates": []})

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
                        name="document_templates",
                        json_args='{"request":{"action":"list"}}',
                    )
                }
            elif len(returned) == 1:
                self.assertNotIn("document_templates", visible)
                clock["now"] = 60.0
                yield {
                    0: DeltaToolCall(
                        name="graph", json_args='{"entity_id":"entity"}'
                    )
                }
            elif len(returned) == 2:
                self.assertIn("document_templates", visible)
                yield {
                    0: DeltaToolCall(
                        name="document_templates",
                        json_args='{"request":{"action":"list"}}',
                    )
                }
            else:
                yield "The service is available again."

        with patch(
            "galadril_scribe.extensions.monotonic",
            side_effect=lambda: clock["now"],
            create=True,
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(gateway)
            ) as client:
                runtime = Runtime(
                    settings(),
                    client,
                    model=FunctionModel(stream_function=response),
                )
                chunks = [
                    chunk
                    async for chunk in runtime.stream(
                        RunRequest(
                            prompt="Continue",
                            capability="a" * 64,
                            library_enabled=True,
                        )
                    )
                ]
        self.assertEqual(chunks[-1].kind, "completed")
        self.assertEqual(
            calls, ["document_templates", "graph", "document_templates"]
        )

    async def test_unavailable_clients_are_rediscovered_when_service_returns(
        self,
    ) -> None:
        for phase in ("factory", "enter", "discover"):
            with self.subTest(phase=phase):
                await self._exercise_recovery(phase)

    async def _exercise_recovery(self, phase: str) -> None:
        state = {"now": 0.0, "available": False}
        effects: list[str] = []

        async def build() -> str:
            effects.append("execute")
            return "Verified restored service"

        class RecoveringService(FunctionToolset[Dependencies]):
            async def __aenter__(self) -> Self:
                if phase == "enter" and not state["available"]:
                    raise OSError("private connection detail")
                return self

            async def get_tools(
                self, ctx: RunContext[Dependencies]
            ) -> dict[str, ToolsetTool[Dependencies]]:
                if phase == "discover" and not state["available"]:
                    raise OSError("private connection detail")
                return await super().get_tools(ctx)

        def factory() -> FunctionToolset[Dependencies]:
            if phase == "factory" and not state["available"]:
                raise OSError("private connection detail")
            return RecoveringService(tools=[build])

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
                self.assertNotIn("optional_algorithm_service_build", visible)
                state.update(now=60.0, available=True)
                yield {
                    0: DeltaToolCall(
                        name="graph", json_args='{"entity_id":"entity"}'
                    )
                }
            elif len(returned) == 1:
                self.assertIn("optional_algorithm_service_build", visible)
                yield {
                    0: DeltaToolCall(
                        name="optional_algorithm_service_build", json_args="{}"
                    )
                }
            else:
                yield "Verified restored service"

        with patch(
            "galadril_scribe.extensions.monotonic",
            side_effect=lambda: float(state["now"]),
            create=True,
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda incoming: httpx.Response(200, json={})
                )
            ) as client:
                runtime = Runtime(
                    settings(),
                    client,
                    model=FunctionModel(stream_function=response),
                    extensions=(ToolExtension(descriptor(), factory),),
                )
                chunks = [
                    chunk
                    async for chunk in runtime.stream(
                        RunRequest(
                            prompt="Continue",
                            capability="a" * 64,
                            tool_ids=(descriptor().id,),
                        )
                    )
                ]
        self.assertEqual(chunks[-1].kind, "completed")
        self.assertEqual(effects, ["execute"])

    async def test_native_gateway_failures_are_results_and_hide_the_failed_tool(
        self,
    ) -> None:
        for status in (401, 403, 404, 429, 503):
            with self.subTest(status=status):
                calls: list[str] = []

                def gateway(
                    incoming: httpx.Request,
                    *,
                    calls: list[str] = calls,
                    status: int = status,
                ) -> httpx.Response:
                    calls.append(json.loads(incoming.content)["operation"])
                    return httpx.Response(status, text="private backend detail")

                async def response(
                    messages: list[ModelMessage], info: AgentInfo
                ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
                    returned = any(
                        isinstance(part, ToolReturnPart)
                        for message in messages
                        if isinstance(message, ModelRequest)
                        for part in message.parts
                    )
                    tools = {tool.name for tool in info.function_tools}
                    if not returned:
                        self.assertIn("document_templates", tools)
                        yield {
                            0: DeltaToolCall(
                                name="document_templates",
                                json_args='{"request":{"action":"list"}}',
                            )
                        }
                    else:
                        self.assertNotIn("document_templates", tools)
                        yield "I can continue and explain the missing document service."

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(gateway)
                ) as client:
                    runtime = Runtime(
                        settings(),
                        client,
                        model=FunctionModel(stream_function=response),
                    )
                    with capture_logs() as logs:
                        chunks = [
                            chunk
                            async for chunk in runtime.stream(
                                RunRequest(
                                    prompt="Compile a document",
                                    capability="a" * 64,
                                    library_enabled=True,
                                )
                            )
                        ]
                self.assertEqual(chunks[-1].kind, "completed")
                self.assertEqual(calls, ["document_templates"])
                self.assertNotIn(
                    "private backend detail", str(chunks) + str(logs)
                )

    async def test_malformed_compiler_response_does_not_claim_success_or_abort(
        self,
    ) -> None:
        async def response(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            returned = any(
                isinstance(part, ToolReturnPart)
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
            )
            if not returned:
                yield {
                    0: DeltaToolCall(
                        name="compile_document",
                        json_args=json.dumps(
                            {
                                "request": {
                                    "documents": [
                                        {
                                            "template_id": "a" * 32,
                                            "version": "b" * 64,
                                        }
                                    ]
                                }
                            }
                        ),
                    )
                }
            else:
                self.assertNotIn(
                    "compile_document",
                    {tool.name for tool in info.function_tools},
                )
                yield "Compilation could not be completed."

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda incoming: httpx.Response(200, json={})
            )
        ) as client:
            runtime = Runtime(
                settings(),
                client,
                model=FunctionModel(stream_function=response),
            )
            chunks = [
                chunk
                async for chunk in runtime.stream(
                    RunRequest(
                        prompt="Compile",
                        capability="a" * 64,
                        library_enabled=True,
                    )
                )
            ]
        self.assertEqual(chunks[-1].kind, "completed")
        self.assertIn(
            "could not be completed", "".join(chunk.content for chunk in chunks)
        )

    async def test_unavailable_extension_discovery_is_hidden_before_model_request(
        self,
    ) -> None:
        for phase in ("enter", "discover", "factory"):
            with self.subTest(phase=phase):

                class MissingService(FunctionToolset[Dependencies]):
                    async def __aenter__(self, *, phase: str = phase) -> Self:
                        if phase == "enter":
                            raise OSError("private connection detail")
                        return self

                    async def get_tools(
                        self, ctx: RunContext[Dependencies]
                    ) -> dict[str, ToolsetTool[Dependencies]]:
                        raise OSError("private connection detail")

                def factory(
                    phase: str = phase,
                    service: type[MissingService] = MissingService,
                ) -> FunctionToolset[Dependencies]:
                    if phase == "factory":
                        raise OSError("private connection detail")
                    return service()

                async def response(
                    messages: list[ModelMessage], info: AgentInfo
                ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
                    self.assertFalse(
                        any(
                            tool.name.startswith("optional_")
                            for tool in info.function_tools
                        )
                    )
                    yield "I can answer with the remaining tools."

                async with httpx.AsyncClient() as client:
                    runtime = Runtime(
                        settings(),
                        client,
                        model=FunctionModel(stream_function=response),
                        extensions=(ToolExtension(descriptor(), factory),),
                    )
                    with capture_logs() as logs:
                        chunks = [
                            chunk
                            async for chunk in runtime.stream(
                                RunRequest(
                                    prompt="Explain",
                                    capability="a" * 64,
                                    tool_ids=(descriptor().id,),
                                )
                            )
                        ]
                self.assertEqual(chunks[-1].kind, "completed")
                self.assertNotIn(
                    "private connection detail", str(chunks) + str(logs)
                )


if __name__ == "__main__":
    unittest.main()
