"""Exercises the external agent loop and its private API boundary."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from typing import Literal

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
    ) == ("I will calculate it. \n\nThe result is 42.")
    activity = [
        json.loads(chunk.content)
        for chunk in chunks
        if chunk.kind == "activity"
    ]
    assert (
        "".join(
            item["data"]
            for item in activity
            if item.get("type") == "python" and item.get("channel") == "code"
        )
        == "6 * 7"
    )
    assert any(
        item.get("type") == "python"
        and item.get("channel") == "status"
        and item.get("data") == "completed"
        for item in activity
    )


@pytest.mark.anyio
async def test_document_search_exposes_verified_eskg_references_to_the_model() -> (
    None
):
    from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    operations: list[str] = []
    reference = {"entityId": "entity", "eventId": "event", "stateType": "legal"}

    def gateway(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        operations.append(payload["operation"])
        if payload["operation"] == "document_search":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "sourceId": "file",
                            "sourceChecksum": "revision",
                            "unitId": "passage",
                            "filename": "Evidence.txt",
                            "text": "Grounded evidence",
                            "page": None,
                            "similarity": 0.7,
                            "relevance": 0.8,
                            "graphScore": 0.1,
                            "eskgRefs": [reference],
                        }
                    ],
                    "warnings": [],
                    "engine": "hipporag2-official",
                    "private_debug": "hidden",
                },
            )
        assert payload == {
            "operation": "graph",
            "entity_id": "entity",
            "depth": 1,
        }
        return httpx.Response(200, json={"states": ["authorized state"]})

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
        if not returned:
            yield {
                0: DeltaToolCall(
                    name="document_search",
                    json_args='{"query":"Connected evidence","mode":"hipporag2"}',
                )
            }
        elif len(returned) == 1:
            content = json.loads(json.dumps(returned[0].content))
            assert isinstance(content, dict)
            assert content["engine"] == "hipporag2-official"
            assert content["hits"][0]["text"] == "Grounded evidence"
            assert content["hits"][0]["eskgRefs"] == [reference]
            assert "private_debug" not in content
            yield {
                0: DeltaToolCall(
                    name="graph", json_args='{"entity_id":"entity"}'
                )
            }
        else:
            yield "Answer grounded in the passage and its authorized state."

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        selected = request().model_copy(update={"library_enabled": True})
        chunks = [chunk async for chunk in runtime.stream(selected)]
    assert chunks[-1].kind == "completed"
    assert operations == ["document_search", "graph"]


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
        assert {
            "web_search",
            "read_file",
            "document_search",
        } <= names
        assert "library" not in names
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


@pytest.mark.parametrize("status", [403, 500])
@pytest.mark.anyio
async def test_unavailable_graph_context_never_reaches_the_model(
    status: int,
) -> None:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    calls: list[dict[str, object]] = []

    async def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        return httpx.Response(status, text="private-graph-canary")

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        assert "private-graph-canary" not in str(messages)
        yield "I can continue without the unavailable graph."

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        selected = RunRequest.model_validate(
            request().model_dump()
            | {"graph_context": {"kind": "entity", "id": "private-entity"}}
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
    assert calls == [
        {"operation": "context", "kind": "entity", "id": "private-entity"}
    ]
    assert chunks[-1].kind == "completed"
    assert "private-graph-canary" not in str(chunks)
    assert any(chunk.kind == "content" for chunk in chunks)
    assert [
        json.loads(chunk.content)
        for chunk in chunks
        if chunk.kind == "activity"
        and json.loads(chunk.content).get("type") == "warning"
    ] == [{"type": "warning", "source": "graph"}]


@pytest.mark.anyio
async def test_invalid_document_search_logs_contract_failure_without_evidence() -> (
    None
):
    def gateway(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": [{"filename": "private-evidence.txt"}],
                "warnings": [],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["document_search"])
        )
        selected = RunRequest.model_validate(
            request().model_dump() | {"library_enabled": True}
        )
        with capture_logs() as logs:
            chunks = [chunk async for chunk in runtime.stream(selected)]
    assert chunks[-1].kind == "completed"
    failures = [
        log
        for log in logs
        if log.get("event") == "scribe.tool.invalid_response"
    ]
    assert len(failures) == 1
    assert failures[0]["operation"] == "document_search"
    assert failures[0]["error_type"] == "ValidationError"
    assert "private-evidence.txt" not in str(logs)


@pytest.mark.anyio
async def test_document_search_returns_only_gateway_scoped_documents() -> None:
    calls: list[dict[str, object]] = []

    async def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        return httpx.Response(
            200,
            json={"hits": [], "warnings": []},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["document_search"])
        )
        selected = RunRequest.model_validate(
            request().model_dump() | {"library_enabled": True}
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
        assert chunks[-1].kind == "completed"
    assert calls and all(
        call.get("operation") == "document_search" for call in calls
    )
    assert all(
        "tenant_id" not in call and "project_id" not in call for call in calls
    )


@pytest.mark.anyio
async def test_document_search_does_not_download_files_before_the_agent_selects_one() -> (
    None
):
    calls: list[dict[str, object]] = []

    def gateway(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        return httpx.Response(
            200,
            json={"hits": [], "warnings": []},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=["document_search"])
        )
        selected = RunRequest.model_validate(
            request().model_dump() | {"library_enabled": True}
        )
        chunks = [chunk async for chunk in runtime.stream(selected)]
    assert chunks[-1].kind == "completed"
    assert all(call["operation"] == "document_search" for call in calls)


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
                            "fixture_mcp_web_web_search"
                            if plugin
                            else "external_0_web_search"
                        ]
                    ),
                    extensions=(
                        ToolExtension(
                            Capability.model_validate(
                                {
                                    "id": "fixture.mcp.web",
                                    "plugin_id": "fixture",
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
                    request().model_copy(
                        update={"tool_ids": ("fixture.mcp.web",)}
                    )
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


@pytest.mark.anyio
@pytest.mark.parametrize("preload", [True, False])
@pytest.mark.parametrize("invalid", [True, False])
async def test_gateway_web_search_reports_only_valid_public_site_metadata(
    preload: bool, invalid: bool
) -> None:
    research = {
        "sites": [
            {
                "origin": "https://example.com/private?secret=1"
                if invalid
                else "https://example.com",
                "result_count": 2,
            }
        ]
    }

    def transport(incoming: httpx.Request) -> httpx.Response:
        assert json.loads(incoming.content)["operation"] == "web"
        return httpx.Response(200, json={"results": [], "research": research})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=[] if preload else ["web_search"]),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                request().model_copy(
                    update={
                        "web_query": "public query" if preload else None,
                        "web_search": True,
                    }
                )
            )
        ]
    activities = [
        json.loads(chunk.content)
        for chunk in chunks
        if chunk.kind == "activity"
    ]
    searches = [item for item in activities if item["type"] == "web_research"]
    assert len(searches) == int(not invalid)
    if not invalid:
        assert searches[0]["sites"] == research["sites"]
    assert "public query" not in json.dumps(searches)


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


@pytest.mark.anyio
async def test_terminal_callback_retries_only_transient_failures_with_the_same_cursor() -> (
    None
):
    from galadril_scribe.background import deliver
    from galadril_scribe.contracts import GenerationRequest

    received: list[dict[str, object]] = []

    async def sink(incoming: httpx.Request) -> httpx.Response:
        received.append(json.loads(incoming.content))
        return httpx.Response(503 if len(received) == 1 else 200)

    selected = GenerationRequest.model_validate(
        request().model_dump() | {"id": "b" * 32}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink)) as client:
        runtime = Runtime(settings(), client, model=TestModel(call_tools=[]))
        await deliver(runtime, selected, 11, "completed")
    assert len(received) == 2
    assert received[0] == received[1]
    assert received[1] == {
        "generationId": selected.id,
        "sequence": 11,
        "kind": "completed",
        "content": "",
    }


@pytest.mark.anyio
async def test_text_stream_is_visible_before_provider_response_finishes() -> (
    None
):
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    released = asyncio.Event()
    first = "A supported introduction is available. " * 20

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        yield first
        await released.wait()
        yield "Final verified answer."

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        stream = runtime.stream(request())
        try:
            async with asyncio.timeout(3):
                while (chunk := await anext(stream)).kind != "content":
                    pass
            assert "supported introduction" in chunk.content
            assert not released.is_set()
            released.set()
            rest = [chunk async for chunk in stream]
            assert rest[-1].kind == "completed"
        finally:
            released.set()
            await stream.aclose()


def test_execution_budgets_distinguish_chat_from_research_without_tool_quotas() -> (
    None
):
    from galadril_scribe.execution import ExecutionBudget

    ordinary = ExecutionBudget.for_request(settings(), request())
    research = ExecutionBudget.for_request(
        settings(), request().model_copy(update={"execution_mode": "research"})
    )
    assert ordinary.limits.tool_calls_limit is None
    assert research.limits.tool_calls_limit is None
    assert research.timeout_seconds >= 8 * 3600
    assert research.limits.request_limit > ordinary.limits.request_limit * 10


def test_memory_compression_preserves_selected_evidence_exactly_and_is_reversible() -> (
    None
):
    from galadril_scribe.execution import ResearchMemory
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    memory = ResearchMemory()
    messages = [
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "web_search",
                    "CE, source officielle, citation exacte à conserver.",
                    "important",
                ),
                ToolReturnPart(
                    "web_search",
                    "Piste secondaire sans rapport. " * 100,
                    "secondary",
                ),
            ]
        )
    ]
    memory.observe(messages)
    memory.compact(
        ("secondary",), ("important",), "Piste sans rapport avec la question."
    )
    compacted = memory.project(messages)
    assert compacted[0].parts[0].content == messages[0].parts[0].content
    assert len(compacted[0].parts[1].content) < 200
    assert memory.restore("secondary") == messages[0].parts[1].content
    with pytest.raises(ValueError):
        memory.compact(("important",), (), "Remove relevant evidence")


@pytest.mark.anyio
async def test_native_step_store_recovers_tool_effects_after_recreation() -> (
    None
):
    from galadril_scribe.step_store import GatewayStepStore
    from pydantic import JsonValue
    from pydantic_ai_harness.step_persistence import ToolEffectRecord

    persisted: dict[str, JsonValue] = {}

    async def access(key: str, value: JsonValue | None) -> JsonValue:
        if value is not None:
            persisted[key] = value
            return True
        return persisted.get(key)

    async def snapshot(snapshot: object) -> None:
        return None

    first = GatewayStepStore(access, snapshot)
    await first.record_tool_effect(
        ToolEffectRecord(
            run_id="run",
            tool_call_id="call",
            tool_name="web_search",
            status="started",
        )
    )
    restored = GatewayStepStore(access, snapshot)
    pending = await restored.list_unresolved_tool_effects(run_id="run")
    assert len(pending) == 1 and pending[0].tool_name == "web_search"
    await restored.record_tool_effect(
        ToolEffectRecord(
            run_id="run",
            tool_call_id="call",
            tool_name="web_search",
            status="completed",
        )
    )
    assert (
        await GatewayStepStore(access, snapshot).list_unresolved_tool_effects(
            run_id="run"
        )
        == []
    )


def test_streaming_redaction_is_independent_of_token_fragment_boundaries() -> (
    None
):
    from galadril_scribe.builtin import SecretPolicy
    from galadril_scribe.execution import StreamingText

    values = (
        (
            "Prefix private token with spaces suffix.",
            ("private token with spaces",),
        ),
        ("Prefix sk-" + "x" * 32 + " suffix.", ()),
        (
            "Prefix -----BEGIN PRIVATE KEY-----\nYWJjZGVm\n-----END PRIVATE KEY----- suffix.",
            (),
        ),
        ("Texte français avec une référence vérifiée.", ()),
    )
    for original, secrets in values:
        policy = SecretPolicy(secrets)
        for split in range(len(original) + 1):
            stream = StreamingText(policy)
            actual = (
                stream.feed(original[:split])
                + stream.feed(original[split:])
                + stream.feed("", final=True)
            )
            assert actual == policy.public_text(original), (split, actual)


@pytest.mark.anyio
async def test_durable_run_resumes_after_completed_tools_without_replaying_them() -> (
    None
):
    from galadril_scribe.contracts import GenerationRequest
    from galadril_scribe.execution import ExecutionState
    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    stored: dict[str, JsonValue] = {}
    waiting = asyncio.Event()
    calls = 0

    def gateway(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert payload["operation"] == "agent_state"
        if payload.get("value") is not None:
            stored[payload["key"]] = payload["value"]
            return httpx.Response(200, json=True)
        return httpx.Response(
            200, content=json.dumps(stored.get(payload["key"]))
        )

    async def initial(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        nonlocal calls
        calls += 1
        if any(
            isinstance(part, ToolReturnPart)
            for message in messages
            for part in message.parts
        ):
            waiting.set()
            await asyncio.Event().wait()
        yield {
            0: DeltaToolCall(
                name="date_interval",
                json_args='{"start":"2026-11-01","end":"2026-11-03"}',
            )
        }

    async def continued(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        assert any(
            isinstance(message, ModelRequest)
            and any(
                isinstance(part, ToolReturnPart)
                and part.tool_name == "date_interval"
                for part in message.parts
            )
            for message in messages
        )
        yield "The verified interval is two days."

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        selected = GenerationRequest(
            prompt="Compare dates", capability="a" * 64, id="b" * 32
        )
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=initial)
        )

        async def consume() -> None:
            async for _chunk in runtime.stream(selected):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert isinstance(stored["checkpoint"], str)
        state = ExecutionState.model_validate_json(stored["checkpoint"])
        assert state.safe
        assert not state.web_requires_approval
        restarted = Runtime(
            settings(), client, model=FunctionModel(stream_function=continued)
        )
        chunks = [
            chunk
            async for chunk in restarted.stream(
                selected.model_copy(
                    update={
                        "id": "c" * 32,
                        "resume_state": state.model_dump_json(),
                        "resume_source": selected.id,
                    }
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert calls == 2
    assert "two days" in "".join(
        chunk.content for chunk in chunks if chunk.kind == "content"
    )


def test_checkpoint_compression_is_lossless_and_rejects_decompression_bombs() -> (
    None
):
    from galadril_scribe.execution import pack_text, unpack_text

    text = "Citation exacte, date et référence vérifiée.\n" * 20000
    packed = pack_text(text)
    assert len(packed) < len(text) // 4
    assert unpack_text(packed) == text
    with pytest.raises(ValueError):
        unpack_text(pack_text("a" * (8 * 1024 * 1024 + 1)))


def test_checkpoints_redact_credentials_without_exposing_provider_reasoning() -> (
    None
):
    from galadril_scribe.builtin import SecretPolicy
    from galadril_scribe.execution_memory import checkpoint_messages
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        ModelResponse,
        TextPart,
    )

    policy = SecretPolicy(("private-token-for-fixture",))
    encoded = checkpoint_messages(
        [ModelResponse(parts=[TextPart("Result private-token-for-fixture")])],
        redact=policy.public_text,
    )
    assert "private-token-for-fixture" not in encoded
    restored = ModelMessagesTypeAdapter.validate_json(encoded)
    assert restored[0].parts[0].content == "Result [REDACTED]"


def test_pending_read_tools_can_be_replayed_but_unknown_effects_cannot() -> (
    None
):
    from galadril_scribe.execution import safe_to_replay
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
    )

    web = ModelResponse(
        parts=[ToolCallPart("web_search", {"query": "public case law"}, "web")]
    )
    send = ModelResponse(parts=[ToolCallPart("send_email", {}, "send")])
    assert safe_to_replay([web])
    assert not safe_to_replay([web, send])
    assert safe_to_replay(
        [
            send,
            ModelRequest(parts=[ToolReturnPart("send_email", "sent", "send")]),
        ]
    )


@pytest.mark.anyio
async def test_recovery_replays_a_pending_read_tool_before_requesting_the_model() -> (
    None
):
    from galadril_scribe.contracts import GenerationRequest
    from galadril_scribe.execution import ExecutionState, pack_text
    from pydantic_ai.messages import (
        ModelMessage,
        ModelMessagesTypeAdapter,
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    history = [
        ModelRequest(parts=[UserPromptPart("Compare dates")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    "date_interval",
                    {"start": "2026-11-01", "end": "2026-11-03"},
                    "pending_read",
                )
            ]
        ),
    ]
    state = ExecutionState(
        messages=pack_text(
            ModelMessagesTypeAdapter.dump_json(history).decode()
        ),
        requests=1,
        tool_calls=0,
        input_tokens=10,
        output_tokens=5,
        elapsed_seconds=1,
        safe=True,
        mission="Verify dates",
    )

    async def resumed(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        assert any(
            isinstance(part, ToolReturnPart)
            and part.tool_call_id == "pending_read"
            for message in messages
            for part in message.parts
        )
        yield "Verified two days."

    def gateway(incoming: httpx.Request) -> httpx.Response:
        assert json.loads(incoming.content)["operation"] == "agent_state"
        return httpx.Response(200, content="null")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=resumed)
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                GenerationRequest(
                    id="d" * 32,
                    prompt="Continue",
                    capability="a" * 64,
                    resume_state=state.model_dump_json(),
                    resume_source="b" * 32,
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert "Verified two days." == "".join(
        chunk.content for chunk in chunks if chunk.kind == "content"
    )


@pytest.mark.anyio
async def test_conversation_source_accepts_only_published_message_shapes() -> (
    None
):
    from galadril_scribe.conversation_memory import ConversationSource
    from pydantic import JsonValue, ValidationError

    async def query(identifier: str | None) -> JsonValue:
        if identifier is None:
            return ["chat-a"]
        return {
            "messages": [
                {
                    "role": "assistant",
                    "content": "Published finding",
                    "private_snapshot": "must not appear",
                }
            ]
        }

    source = ConversationSource(query)
    assert [run.run_id for run in await source.list_runs()] == ["chat-a"]
    with pytest.raises(ValidationError):
        await source.run_history(run_id="chat-a")


@pytest.mark.parametrize("outcome", ["failed", "interrupted", "retried"])
def test_uncertain_tool_outcomes_require_review_only_for_external_effects(
    outcome: Literal["failed", "interrupted", "retried"],
) -> None:
    from galadril_scribe.execution import safe_to_replay
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
    )

    for tool, expected in [("web_search", True), ("send_email", False)]:
        messages = [
            ModelResponse(parts=[ToolCallPart(tool, {}, "pending")]),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool, "Unknown outcome", "pending", outcome=outcome
                    )
                ]
            ),
        ]
        assert safe_to_replay(messages) is expected


@pytest.mark.anyio
@pytest.mark.parametrize("restored", [False, True])
async def test_private_memory_requires_web_approval_after_recall_or_recovery(
    restored: bool,
) -> None:
    from galadril_scribe.execution import ExecutionState, pack_text
    from pydantic_ai.messages import (
        ModelMessage,
        ModelMessagesTypeAdapter,
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    outbound: list[str] = []
    recalled: list[str | None] = []
    returns: list[str] = []
    history = [
        ModelRequest(parts=[UserPromptPart("Verify private findings")]),
        ModelResponse(parts=[ToolCallPart("date_interval", {}, "previous")]),
        ModelRequest(
            parts=[
                ToolReturnPart("date_interval", "Private finding", "previous")
            ]
        ),
    ]
    state = ExecutionState(
        messages=pack_text(
            ModelMessagesTypeAdapter.dump_json(history).decode()
        ),
        requests=1,
        tool_calls=1,
        input_tokens=10,
        output_tokens=5,
        elapsed_seconds=1,
        safe=True,
        mission="Verify private findings",
    )

    def gateway(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        if payload["operation"] == "conversation_memory":
            recalled.append(payload["id"])
            value = (
                ["chat-a"]
                if payload["id"] is None
                else {
                    "messages": [
                        {"role": "assistant", "content": "Private finding"}
                    ]
                }
            )
            return httpx.Response(200, json=value)
        assert payload["operation"] == "web"
        outbound.append(payload["query"])
        return httpx.Response(200, json={"answer": "Public result"})

    async def model(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        completed = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        web = next(
            (part for part in completed if part.tool_name == "web_search"), None
        )
        if web is not None:
            returns.append(web.model_response_str())
            yield "Continue with private evidence only."
        elif not restored and not any(
            part.tool_name == "search_conversation_history"
            for part in completed
        ):
            yield {
                0: DeltaToolCall(
                    name="search_conversation_history",
                    json_args='{"query":"Private finding"}',
                )
            }
        else:
            yield {
                0: DeltaToolCall(
                    name="web_search", json_args='{"query":"Private finding"}'
                )
            }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings().model_copy(update={"web_search_provider": "openai"}),
            client,
            model=FunctionModel(stream_function=model),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Verify private findings",
                    capability="a" * 64,
                    web_search=True,
                    conversation_memory=not restored,
                    resume_state=state.model_dump_json() if restored else None,
                    resume_source="b" * 32 if restored else None,
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert bool(recalled) is not restored
    assert outbound == []
    assert len(returns) == 1
    assert "not authorized" in returns[0]
