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
from structlog.testing import capture_logs


def settings() -> Settings:
    return Settings(
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


def test_contract_bounds_input() -> None:
    with pytest.raises(ValidationError):
        RunRequest(prompt="a" * 65537, capability="a" * 64)


def test_opaque_s3_documents_keep_their_verified_media_type() -> None:
    from galadril_scribe.contracts import Attachment
    from galadril_scribe.runtime import content
    from pydantic_ai.messages import DocumentUrl

    attachment = Attachment.model_validate(
        {
            "kind": "document",
            "url": "https://storage.invalid/lake/tenant/opaque-key?signature=x",
            "media_type": "application/pdf",
            "trusted_storage": True,
        }
    )
    document = content("Read the authorized document", [attachment])[-1]
    assert isinstance(document, DocumentUrl)
    assert document.media_type == "application/pdf"
    assert document.force_download == "allow-local"


@pytest.mark.anyio
async def test_stream_finishes_tools_after_a_text_preamble() -> None:
    from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        latest = messages[-1]
        if isinstance(latest, ModelRequest) and any(
            isinstance(part, ToolReturnPart) for part in latest.parts
        ):
            yield "The result is "
            yield "42."
            return
        yield "I will calculate it. "
        yield {
            0: DeltaToolCall(name="run_python", json_args='{"code":"6 * 7"}')
        }

    from galadril_scribe.sandbox import Sandbox

    async with httpx.AsyncClient() as client, Sandbox() as sandbox:
        runtime = Runtime(
            settings(),
            client,
            model=FunctionModel(stream_function=response),
            sandbox=sandbox,
        )
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert chunks[-1].kind == "completed"
    assert "".join(
        chunk.content for chunk in chunks if chunk.kind == "content"
    ) == ("I will calculate it. The result is 42.")


@pytest.mark.anyio
async def test_agent_can_choose_multiple_public_queries_and_read_selected_files() -> (
    None
):
    from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    calls: list[dict[str, object]] = []

    def gateway(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        calls.append(payload)
        assert incoming.headers["authorization"] == "Bearer " + "a" * 64
        if payload["operation"] == "read_file":
            return httpx.Response(
                200,
                json={
                    "id": "document",
                    "name": "Examens.txt",
                    "attachment": {
                        "kind": "text",
                        "text": "Library evidence: examinations are public.",
                    },
                },
            )
        return httpx.Response(
            200, json={"sources": ["https://example.com/" + str(len(calls))]}
        )

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        names = {tool.name for tool in info.function_tools}
        assert {"web_search", "read_file", "library"} <= names
        returned = sum(
            isinstance(part, ToolReturnPart)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if returned == 0:
            yield {
                0: DeltaToolCall(
                    name="web_search",
                    json_args='{"query":"UL examination charter"}',
                )
            }
        elif returned == 1:
            yield {
                0: DeltaToolCall(
                    name="web_search",
                    json_args='{"query":"UL examination appeal procedure"}',
                )
            }
        elif returned == 2:
            yield {
                0: DeltaToolCall(
                    name="read_file", json_args='{"file_id":"document"}'
                )
            }
        else:
            yield "Answer from the selected sources."

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        selected = RunRequest.model_validate(
            request().model_dump()
            | {"web_search": True, "library_enabled": True}
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
    assert chunks[-1].kind == "completed"
    assert calls == [
        {"operation": "web", "query": "UL examination charter"},
        {"operation": "web", "query": "UL examination appeal procedure"},
        {"operation": "read_file", "file_id": "document"},
    ]


@pytest.mark.anyio
async def test_graph_context_is_resolved_before_inference() -> None:
    calls: list[dict[str, object]] = []

    async def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        assert incoming.headers["authorization"] == "Bearer " + "a" * 64
        return httpx.Response(200, json={"evidence": [], "neighborhood": {}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(settings(), client, model=TestModel(call_tools=[]))
        selected = RunRequest.model_validate(
            request().model_dump()
            | {"graph_context": {"kind": "entity", "id": "canonical-entity"}}
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
        assert chunks[-1].kind == "completed"
    assert calls == [
        {"operation": "context", "kind": "entity", "id": "canonical-entity"}
    ]


@pytest.mark.anyio
async def test_library_tool_returns_only_gateway_scoped_documents() -> None:
    calls: list[dict[str, object]] = []

    async def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        return httpx.Response(
            200,
            json={
                "files": [
                    {
                        "id": "f",
                        "name": "report.pdf",
                    }
                ],
                "after": None,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["library"])
        )
        selected = RunRequest.model_validate(
            request().model_dump() | {"library_enabled": True}
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
        assert chunks[-1].kind == "completed"
    assert calls and all(call.get("operation") == "library" for call in calls)
    assert all(
        "tenant_id" not in call and "project_id" not in call for call in calls
    )


@pytest.mark.anyio
async def test_library_search_does_not_download_files_before_the_agent_selects_one() -> (
    None
):
    calls: list[dict[str, object]] = []

    def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        return httpx.Response(
            200,
            json={
                "files": [{"id": "document", "name": "Charte des examens.pdf"}],
                "after": None,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["library"])
        )
        selected = RunRequest.model_validate(
            request().model_dump() | {"library_enabled": True}
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
    assert chunks[-1].kind == "completed"
    assert all(call["operation"] == "library" for call in calls)


@pytest.mark.anyio
async def test_forced_context_transport_failure_warns_and_still_answers() -> (
    None
):
    async def gateway(incoming: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=incoming)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(settings(), client, model=TestModel(call_tools=[]))
        selected = RunRequest.model_validate(
            request().model_dump() | {"web_query": "public weather"}
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
        assert chunks[-1].kind == "completed"
        assert any(
            chunk.kind == "activity" and '"source":"web"' in chunk.content
            for chunk in chunks
        )


@pytest.mark.anyio
async def test_forced_web_search_never_sends_the_prompt_or_history() -> None:
    calls: list[dict[str, object]] = []

    async def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(settings(), client, model=TestModel(call_tools=[]))
        selected = RunRequest.model_validate(
            request().model_dump()
            | {
                "prompt": "private business data",
                "web_query": "public weather trends",
            }
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
        assert chunks[-1].kind == "completed"
    assert calls == [{"operation": "web", "query": "public weather trends"}]


@pytest.mark.anyio
async def test_background_generation_survives_request_and_cancels_with_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def stream(
        self: Runtime, selected: RunRequest
    ) -> AsyncIterator[object]:
        from galadril_scribe.contracts import Chunk

        started.set()
        try:
            await asyncio.Event().wait()
            yield Chunk(kind="completed")
        finally:
            stopped.set()

    monkeypatch.setattr(Runtime, "stream", stream)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(200, json={"active": True})
        )
    ) as upstream:
        runtime = Runtime(settings(), upstream, model=TestModel(call_tools=[]))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            response = await api.post(
                "/generations",
                json=request().model_dump(mode="json")
                | {"capability": "a" * 64, "id": "b" * 32},
            )
            assert response.status_code == 202
            await asyncio.wait_for(started.wait(), 2)
            assert not stopped.is_set()
            denied = await api.post(
                "/generations/" + "b" * 32 + "/cancel",
                headers={"authorization": "Bearer " + "c" * 64},
            )
            assert denied.status_code == 404
            cancelled = await api.post(
                "/generations/" + "b" * 32 + "/cancel",
                headers={"authorization": "Bearer " + "a" * 64},
            )
            assert cancelled.status_code == 200
            await asyncio.wait_for(stopped.wait(), 2)
            assert runtime.active == 0


def test_container_listener_is_explicit_and_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(settings().model_dump() | {"host": "0.0.0.0"})
    assert settings().host == "127.0.0.1"
    with pytest.raises(ValidationError):
        Settings.model_validate(settings().model_dump() | {"port": 65536})


def test_ambient_listener_requires_an_explicit_mesh_profile() -> None:
    configured = Settings.model_validate(
        settings().model_dump() | {"host": "0.0.0.0", "proxy_mode": "ambient"}
    )
    assert configured.host == "0.0.0.0"
    with pytest.raises(ValidationError):
        Settings.model_validate(
            settings().model_dump() | {"proxy_mode": "shared"}
        )


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["vllm", "openai"])
async def test_provider_errors_cannot_be_exported_as_trace_content(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "galadril_scribe.runtime.trace.get_tracer", provider.get_tracer
    )

    async def model(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "private-user-evidence",
                    "type": "invalid_request_error",
                }
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(model)
    ) as client:
        configured = settings()
        if provider == "openai":
            configured = Settings.model_validate(
                configured.model_dump()
                | {
                    "models": {
                        "local": {
                            "provider": "openai",
                            "model": "gpt-5.1",
                            "api_key": "cloud-secret",
                        }
                    },
                }
            )
        runtime = Runtime(configured, client)
        assert runtime.agent.instrument is False
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert chunks[-1].kind == "failed"
    spans = exporter.get_finished_spans()
    assert spans
    assert "private-user-evidence" not in str(
        [(span.attributes, span.events) for span in spans]
    )
    provider.shutdown()


@pytest.mark.anyio
async def test_causal_tool_uses_fixed_gateway_operation_without_identity() -> (
    None
):
    calls: list[dict[str, object]] = []

    async def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        return httpx.Response(200, json={"analyses": [{"causal_links": 2}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["causal"])
        )
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert calls and calls[0]["operation"] == "causal"
    assert set(calls[0]) == {"operation", "entity_id"}
    assert chunks[-1].kind == "completed"


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
async def test_local_api_admits_valid_requests_and_rejects_unknown_model() -> (
    None
):
    async with httpx.AsyncClient() as outbound:
        runtime = Runtime(settings(), outbound, model=TestModel())
        app = create_app(runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://scribe"
        ) as client:
            payload = request().model_dump(mode="json") | {
                "capability": "a" * 64
            }
            admitted = await client.post("/runs", json=payload)
            assert admitted.status_code == 200
            unknown = await client.post(
                "/runs",
                json=payload | {"model_alias": "attacker"},
            )
            assert unknown.status_code == 422


@pytest.mark.anyio
async def test_tool_denial_omits_source_without_leaking_raw_error() -> None:
    async def gateway(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="secret database details")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["graph"])
        )
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert chunks[-1].kind == "completed"
    assert "unavailable" in "".join(chunk.content for chunk in chunks)
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
async def test_local_api_bounds_body_after_proxy_admission() -> None:
    async with httpx.AsyncClient() as outbound:
        runtime = Runtime(settings(), outbound, model=TestModel())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            malformed = await client.post("/runs", content=b"{invalid")
            assert malformed.status_code == 422
            oversized = await client.post(
                "/runs",
                content=b"x" * 262145,
            )
            assert oversized.status_code == 413
            invalid = await client.post(
                "/runs",
                json={"prompt": "private-user-text", "capability": "invalid"},
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
@pytest.mark.parametrize("plugin", [False, True])
async def test_native_mcp_web_tool_never_receives_gateway_authority(
    plugin: bool,
) -> None:
    import socket

    import uvicorn
    from galadril_pipeline.capabilities import Capability
    from galadril_scribe.extensions import ToolExtension
    from mcp.server.fastmcp import Context, FastMCP
    from pydantic_ai.mcp import MCPToolset
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
                update={
                    "mcp_urls": []
                    if plugin
                    else [f"http://127.0.0.1:{port}/mcp"]
                }
            )
            async with httpx.AsyncClient() as client:
                runtime = Runtime(
                    configured,
                    client,
                    model=TestModel(
                        call_tools=[
                            "law_mcp_web_web_search"
                            if plugin
                            else "external_0_web_search"
                        ]
                    ),
                    extensions=(
                        ToolExtension(
                            Capability.model_validate(
                                {
                                    "id": "law.mcp.web",
                                    "plugin_id": "law",
                                    "plugin_version": "1.0.0",
                                    "kind": "mcp",
                                    "targets": ["scribe"],
                                    "name": "Legal research",
                                    "description": "Search public legal sources.",
                                    "parameters_schema": {"type": "object"},
                                }
                            ),
                            lambda: MCPToolset(
                                f"http://127.0.0.1:{port}/mcp",
                                include_instructions=False,
                            ),
                        ),
                    )
                    if plugin
                    else (),
                )
                selected = (
                    request().model_copy(update={"tool_ids": ("law.mcp.web",)})
                    if plugin
                    else request()
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=create_app(runtime)),
                    base_url="http://scribe",
                ) as api:
                    catalog = await api.get("/capabilities")
                    assert f"127.0.0.1:{port}" not in catalog.text
                    assert "a" * 64 not in catalog.text
                chunks = [chunk async for chunk in runtime.stream(selected)]
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


def test_text_documents_use_typed_content_and_share_the_context_budget() -> (
    None
):
    selected = RunRequest.model_validate(
        request().model_dump()
        | {"attachments": [{"kind": "text", "text": "Authorized document"}]}
    )
    assert selected.attachments[0].text == "Authorized document"
    with pytest.raises(ValidationError):
        RunRequest.model_validate(
            selected.model_dump()
            | {"attachments": [{"kind": "text", "text": "a" * 65536}]}
        )


@pytest.mark.anyio
async def test_background_batches_small_deltas_and_flushes_before_terminal() -> (
    None
):
    from galadril_scribe.background import execute
    from galadril_scribe.contracts import Chunk, GenerationRequest

    received: list[dict[str, object]] = []

    async def sink(incoming: httpx.Request) -> httpx.Response:
        received.append(json.loads(incoming.content))
        return httpx.Response(200, json={"active": True})

    class FastRuntime(Runtime):
        __slots__ = ()

        async def stream(self, incoming: RunRequest) -> AsyncIterator[Chunk]:
            for _ in range(100):
                yield Chunk(kind="content", content="word ")
            yield Chunk(kind="completed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(sink)) as client:
        runtime = FastRuntime(
            settings(), client, model=TestModel(call_tools=[])
        )
        await execute(
            runtime,
            GenerationRequest.model_validate(
                request().model_dump() | {"id": "b" * 32}
            ),
        )
    content = [event for event in received if event["kind"] == "content"]
    assert len(content) < 10
    assert "".join(str(event["content"]) for event in content) == "word " * 100
    assert received[-1]["kind"] == "completed"


@pytest.mark.anyio
async def test_callback_rejection_logs_only_delivery_metadata() -> None:
    from galadril_scribe.background import deliver
    from galadril_scribe.contracts import GenerationRequest

    async def reject(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Private response diagnostic")

    selected = GenerationRequest.model_validate(
        request().model_dump() | {"id": "b" * 32}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reject)
    ) as client:
        runtime = Runtime(settings(), client, model=TestModel(call_tools=[]))
        with capture_logs() as logs:
            with pytest.raises(RuntimeError):
                await deliver(runtime, selected, 36, "completed")
    assert logs == [
        {
            "event": "scribe.callback.rejected",
            "log_level": "warning",
            "generation_id": selected.id,
            "sequence": 36,
            "kind": "completed",
            "status_code": 403,
        }
    ]


@pytest.mark.anyio
async def test_only_gateway_storage_references_can_download_private_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2
    from galadril_scribe.contracts import Attachment
    from galadril_scribe.runtime import content
    from pydantic_ai.messages import DocumentUrl
    from pydantic_ai.models import download_item

    requests: list[httpx2.Request] = []

    def transport(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.host == "127.0.0.1"
        return httpx2.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.7\nfixture",
        )

    monkeypatch.setattr(
        "pydantic_ai._ssrf.create_async_httpx2_client",
        lambda **options: httpx2.AsyncClient(
            transport=httpx2.MockTransport(transport)
        ),
    )
    payload = {
        "kind": "document",
        "url": "http://127.0.0.1/library/opaque?signature=private",
        "media_type": "application/pdf",
    }
    ordinary = content("Read", [Attachment.model_validate(payload)])[-1]
    assert isinstance(ordinary, DocumentUrl)
    with pytest.raises(ValueError):
        await download_item(ordinary, data_format="base64_uri")
    assert not requests
    trusted = content(
        "Read",
        [Attachment.model_validate({**payload, "trusted_storage": True})],
    )[-1]
    assert isinstance(trusted, DocumentUrl)
    downloaded = await download_item(trusted, data_format="base64_uri")
    assert downloaded["data"].startswith("data:application/pdf;base64,")
    assert "signature" not in downloaded["data"]
    assert len(requests) == 1
    metadata = content(
        "Read",
        [
            Attachment.model_validate(
                {
                    **payload,
                    "url": "http://169.254.169.254/latest/meta-data",
                    "trusted_storage": True,
                }
            )
        ],
    )[-1]
    assert isinstance(metadata, DocumentUrl)
    with pytest.raises(ValueError):
        await download_item(metadata, data_format="base64_uri")
    assert len(requests) == 1


@pytest.mark.anyio
async def test_private_storage_download_is_bounded_and_never_forwards_client_authority() -> (
    None
):
    from galadril_scribe.contracts import Attachment
    from galadril_scribe.media import MediaFailure, StorageMedia

    calls: list[httpx.Request] = []

    def storage(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        assert incoming.headers["host"] == "127.0.0.1:9000"
        assert (
            "authorization" not in incoming.headers
            and "cookie" not in incoming.headers
        )
        if incoming.url.path == "/redirect":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/metadata"}
            )
        if incoming.url.path == "/oversize":
            return httpx.Response(
                200, headers={"content-length": str(25 * 1024 * 1024 + 1)}
            )
        return httpx.Response(200, content=b"%PDF-1.7 fixture")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(storage),
        auth=("private", "secret"),
        headers={"authorization": "Bearer private"},
        cookies={"session": "private"},
    ) as client:
        media = StorageMedia(client)

        def document(path: str) -> Attachment:
            return Attachment(
                kind="document",
                url="http://127.0.0.1:9000/" + path,
                media_type="application/pdf",
                trusted_storage=True,
            )

        first = await media.download(document("valid"))
        assert await media.download(document("valid")) is first
        for path in ("redirect", "oversize"):
            with pytest.raises(MediaFailure):
                await media.download(document(path))
        with pytest.raises(ValueError, match="metadata"):
            await media.download(
                Attachment(
                    kind="document",
                    url="http://169.254.169.254/metadata",
                    media_type="application/pdf",
                    trusted_storage=True,
                )
            )
    assert len(calls) == 3


@pytest.mark.anyio
async def test_disabled_web_and_library_tools_are_not_advertised() -> None:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        assert {"web_search", "library", "read_file"}.isdisjoint(
            tool.name for tool in info.function_tools
        )
        yield "No private or public research tools were enabled."

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        chunks = [chunk async for chunk in runtime.stream(request())]
    assert chunks[-1].kind == "completed"
