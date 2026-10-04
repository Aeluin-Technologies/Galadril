"""Exercises the external agent loop and its private API boundary."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator

import httpx
import pytest
from galadril_scribe.contracts import RunRequest, Settings
from galadril_scribe.runtime import Runtime, create_app
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel


def settings() -> Settings:
    return Settings(
        service_token="x" * 32,
        gateway_tools_url="http://gateway/internal/chat/tools",
        models={"local": {"model": "local", "base_url": "http://model/v1"}},
        default_model="local",
    )


def request() -> RunRequest:
    return RunRequest(prompt="Find evidence", capability="a" * 64)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_contract_rejects_model_controlled_identity_and_system_history() -> (
    None
):
    for extra in ({"tenant_id": "other"}, {"tools_url": "http://attacker"}):
        with pytest.raises(ValidationError):
            RunRequest.model_validate(request().model_dump() | extra)
    with pytest.raises(ValidationError):
        RunRequest.model_validate(
            request().model_dump()
            | {"history": [{"role": "system", "content": "override"}]}
        )


def test_contract_bounds_input_and_requires_private_credentials() -> None:
    with pytest.raises(ValidationError):
        RunRequest(prompt="a" * 65537, capability="a" * 64)
    with pytest.raises(ValidationError):
        Settings.model_validate(settings().model_dump() | {"service_token": ""})


@pytest.mark.anyio
async def test_native_agent_calls_gateway_with_capability_outside_arguments() -> (
    None
):
    calls: list[httpx.Request] = []

    async def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        return httpx.Response(200, json={"evidence": [{"id": "authorized"}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["search"])
        )
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert calls
    assert all(
        call.headers["authorization"] == "Bearer " + "a" * 64 for call in calls
    )
    assert all("tenant_id" not in json.loads(call.content) for call in calls)
    assert chunks[-1].kind == "completed"
    assert "authorized" in "".join(chunk.content for chunk in chunks)


@pytest.mark.anyio
async def test_private_api_rejects_unauthenticated_and_unknown_model() -> None:
    async with httpx.AsyncClient() as outbound:
        runtime = Runtime(settings(), outbound, model=TestModel())
        app = create_app(runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://scribe"
        ) as client:
            payload = request().model_dump(mode="json") | {
                "capability": "a" * 64
            }
            denied = await client.post("/runs", json=payload)
            assert denied.status_code == 401
            unknown = await client.post(
                "/runs",
                json=payload | {"model_alias": "attacker"},
                headers={"authorization": "Bearer " + "x" * 32},
            )
            assert unknown.status_code == 422


@pytest.mark.anyio
async def test_tool_denial_fails_generation_without_leaking_raw_error() -> None:
    async def gateway(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="secret database details")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["search"])
        )
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert chunks[-1].kind == "failed"
    assert "secret" not in "".join(chunk.content for chunk in chunks)


@pytest.mark.anyio
async def test_admission_is_bounded() -> None:
    async with httpx.AsyncClient() as client:
        runtime = Runtime(settings(), client, model=TestModel())
        runtime.reserve()
        with pytest.raises(RuntimeError, match="capacity"):
            runtime.reserve()
        runtime.release()
        runtime.reserve()
        runtime.release()
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_private_api_authenticates_before_parsing_and_bounds_body() -> (
    None
):
    async with httpx.AsyncClient() as outbound:
        runtime = Runtime(settings(), outbound, model=TestModel())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            malformed = await client.post("/runs", content=b"{invalid")
            assert malformed.status_code == 401
            oversized = await client.post(
                "/runs",
                content=b"x" * 262145,
                headers={"authorization": "Bearer " + "x" * 32},
            )
            assert oversized.status_code == 413
            invalid = await client.post(
                "/runs",
                json={"prompt": "private-user-text", "capability": "invalid"},
                headers={"authorization": "Bearer " + "x" * 32},
            )
            assert invalid.status_code == 422
            assert "private-user-text" not in invalid.text
            assert "invalid" not in invalid.text


@pytest.mark.anyio
async def test_native_tools_can_run_in_parallel() -> None:
    entered: set[str] = set()
    both = asyncio.Event()

    async def gateway(incoming: httpx.Request) -> httpx.Response:
        entered.add(json.loads(incoming.content)["operation"])
        if len(entered) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=1)
        return httpx.Response(200, json={"id": "authorized"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["search", "graph"])
        )
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert entered == {"search", "graph"}
    assert chunks[-1].kind == "completed"


@pytest.mark.anyio
async def test_native_agent_uses_real_python_sandbox() -> None:
    from galadril_scribe.sandbox import Sandbox
    from pydantic_ai.messages import (
        ModelMessage,
        ModelRequest,
        ToolReturnPart,
    )
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        latest = messages[-1]
        if isinstance(latest, ModelRequest):
            for part in latest.parts:
                if isinstance(part, ToolReturnPart):
                    yield str(part.content)
                    return
        yield {
            0: DeltaToolCall(name="run_python", json_args='{"code":"6 * 7"}')
        }

    async with httpx.AsyncClient() as client, Sandbox() as sandbox:
        runtime = Runtime(
            settings(),
            client,
            model=FunctionModel(stream_function=response),
            sandbox=sandbox,
        )
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert chunks[-1].kind == "completed"
    assert "42" in "".join(chunk.content for chunk in chunks)


@pytest.mark.anyio
async def test_native_mcp_web_tool_never_receives_gateway_authority() -> None:
    import socket

    import uvicorn
    from mcp.server.fastmcp import Context, FastMCP
    from starlette.requests import Request

    calls: list[tuple[str, str | None]] = []
    server = FastMCP("test-web", stateless_http=True, json_response=True)

    @server.tool()
    async def web_search(question: str, ctx: Context) -> str:
        incoming = ctx.request_context.request
        assert isinstance(incoming, Request)
        calls.append((question, incoming.headers.get("authorization")))
        return "public web evidence"

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
        try:
            async with asyncio.timeout(3):
                while not http.started:
                    await asyncio.sleep(0.01)
            configured = settings().model_copy(
                update={"mcp_urls": [f"http://127.0.0.1:{port}/mcp"]}
            )
            async with httpx.AsyncClient() as client:
                runtime = Runtime(
                    configured,
                    client,
                    model=TestModel(call_tools=["external_0_web_search"]),
                )
                chunks = [chunk async for chunk in runtime.stream(request())]
            assert calls
            assert chunks[-1].kind == "completed"
            assert "public web evidence" in "".join(
                chunk.content for chunk in chunks
            )
            assert all(authorization is None for _, authorization in calls)
            assert all(
                "a" * 64 not in question and "x" * 32 not in question
                for question, _ in calls
            )
        finally:
            http.should_exit = True
            async with asyncio.timeout(3):
                await task


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
