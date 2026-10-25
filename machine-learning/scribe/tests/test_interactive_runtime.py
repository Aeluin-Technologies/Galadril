"""Interactive runs preserve ordered progress without publishing unguarded deltas."""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from galadril_scribe.background import execute
from galadril_scribe.contracts import (
    Attachment,
    GenerationRequest,
    HistoryMessage,
    RunRequest,
    Settings,
)
from galadril_scribe.runtime import Runtime, create_app
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def settings() -> Settings:
    return Settings(
        gateway_tools_url="http://gateway/tools",
        models={"local": {"model": "local", "base_url": "http://model/v1"}},
        default_model="local",
    )


@pytest.mark.anyio
async def test_planning_question_answer_and_more_tools_after_initial_text() -> (
    None
):
    persisted: list[dict[str, object]] = []
    questions: asyncio.Queue[str] = asyncio.Queue()
    terminal = asyncio.Event()

    def gateway(incoming: httpx.Request) -> httpx.Response:
        data = json.loads(incoming.content)
        if incoming.url.path.endswith("/events"):
            persisted.append(data)
            if data["kind"] == "activity":
                activity = json.loads(data["content"])
                if activity["type"] == "question":
                    questions.put_nowait(activity["id"])
            if data["kind"] in {"completed", "failed"}:
                terminal.set()
        return httpx.Response(200, json={"ok": True})

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        assert info.instructions is not None
        assert "Use concise French." in info.instructions
        assert "Cite the project evidence." in info.instructions
        assert "approval" in info.instructions
        calls = sum(
            isinstance(part, ToolReturnPart)
            for message in messages
            for part in message.parts
        )
        if calls == 0:
            yield "I will investigate. "
            yield {
                0: DeltaToolCall(
                    name="write_plan",
                    json_args=json.dumps(
                        {
                            "items": [
                                {
                                    "id": "research",
                                    "content": "Check official sources",
                                    "status": "in_progress",
                                }
                            ]
                        }
                    ),
                )
            }
        elif calls == 1:
            yield "I need your preference. "
            yield {
                0: DeltaToolCall(
                    name="ask_user_question",
                    json_args=json.dumps(
                        {
                            "questions": [
                                {
                                    "header": "Sources",
                                    "question": "Which sources should I use?",
                                    "options": [
                                        {"label": "Official"},
                                        {"label": "All public"},
                                    ],
                                }
                            ]
                        }
                    ),
                )
            }
        elif calls == 2:
            assert "Official" in str(messages)
            yield {
                0: DeltaToolCall(
                    name="update_task_status",
                    json_args=json.dumps(
                        {"task_id": "research", "status": "completed"}
                    ),
                )
            }
        else:
            yield "Final answer with official sources."

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            try:
                assert (
                    await api.post(
                        "/generations",
                        json={
                            "id": "a" * 32,
                            "prompt": "Research",
                            "user_instructions": "Use concise French.",
                            "project_instructions": "Cite the project evidence.",
                            "capability": "b" * 64,
                        },
                    )
                ).status_code == 202
                async with asyncio.timeout(5):
                    question_id = await questions.get()
                payload = {
                    "question_id": question_id,
                    "answers": [
                        {"header": "Sources", "selected": ["Official"]}
                    ],
                }
                denied = await api.post(
                    f"/generations/{'a' * 32}/answers",
                    headers={"authorization": "Bearer " + "c" * 64},
                    json=payload,
                )
                assert denied.status_code == 404
                accepted = await api.post(
                    f"/generations/{'a' * 32}/answers",
                    headers={"authorization": "Bearer " + "b" * 64},
                    json=payload,
                )
                assert accepted.status_code == 200
                async with asyncio.timeout(5):
                    await terminal.wait()
                assert persisted[-1]["kind"] == "completed"
            finally:
                for _, task in tuple(runtime.jobs.values()):
                    task.cancel()
                await asyncio.gather(
                    *(task for _, task in tuple(runtime.jobs.values())),
                    return_exceptions=True,
                )
    events = [event for event in persisted if event["kind"] != "heartbeat"]
    assert [event["sequence"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert (
        "".join(
            str(event["content"])
            for event in events
            if event["kind"] == "content"
        )
        == "I will investigate. I need your preference. Final answer with official sources."
    )
    activities = [
        json.loads(str(event["content"]))
        for event in events
        if event["kind"] == "activity"
    ]
    assert any(
        activity["type"] == "plan" and activity["task"]["status"] == "completed"
        for activity in activities
    )
    assert any(
        activity["type"] == "question_resolved" for activity in activities
    )
    assert "b" * 64 not in json.dumps(events)


@pytest.mark.anyio
async def test_split_provider_secret_never_crosses_the_presentation_boundary() -> (
    None
):
    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        yield "Safe prefix "
        yield "b" * 16
        yield "b" * 48
        yield " safe suffix"

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(prompt="Explain", capability="b" * 64)
            )
        ]
    assert chunks[-1].kind == "completed"
    text = "".join(chunk.content for chunk in chunks if chunk.kind == "content")
    assert "b" * 16 not in text
    assert "Safe prefix" in text and "safe suffix" in text


@pytest.mark.anyio
async def test_guarded_unicode_output_respects_gateway_event_bounds() -> None:
    text = "🌍" * 12000

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        yield text

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(prompt="Explain", capability="b" * 64)
            )
        ]
    assert chunks[-1].kind == "completed"
    content = [chunk.content for chunk in chunks if chunk.kind == "content"]
    assert "".join(content) == text
    assert all(len(part.encode()) <= 16384 for part in content)


@pytest.mark.anyio
async def test_background_batches_fit_gateway_http_body_limit() -> None:
    text = json.dumps(
        {"evidence": ['Quoted "source" with a newline\n🌍'] * 1000},
        ensure_ascii=False,
    )
    bodies: list[bytes] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200 if len(request.content) <= 4096 else 413)

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        yield text

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(gateway)
    ) as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        await execute(
            runtime,
            GenerationRequest(
                id="a" * 32, prompt="Explain", capability="b" * 64
            ),
        )

    events = [json.loads(body) for body in bodies]
    assert events[-1]["kind"] == "completed"
    assert all(len(body) <= 4096 for body in bodies)
    assert (
        "".join(
            event["content"] for event in events if event["kind"] == "content"
        )
        == text
    )
    output = [event for event in events if event["kind"] != "heartbeat"]
    assert [event["sequence"] for event in output] == list(
        range(1, len(output) + 1)
    )


@pytest.mark.anyio
async def test_default_budget_allows_planning_alongside_research() -> None:
    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        calls = sum(
            isinstance(part, ToolReturnPart)
            for message in messages
            for part in message.parts
        )
        if calls < 10:
            yield {0: DeltaToolCall(name="read_plan", json_args="{}")}
        else:
            yield "Research complete."

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(prompt="Research", capability="b" * 64)
            )
        ]
    assert chunks[-1].kind == "completed"
    assert any(chunk.content == "Research complete." for chunk in chunks)


# CRITICAL FIX: web_search takes `query`, just like library.
# The earlier test supplied `question`, causing PydanticAI validation retries.
_OPTIONAL_TOOL_ARGUMENTS = {
    "search": {"question": "Charter"},
    "graph": {"entity_id": "entity"},
    "causal": {"entity_id": "entity"},
    "library": {"query": "Charter"},
    "read_file": {"file_id": "c" * 32},
    "web_search": {"query": "Charter"},
}


@pytest.mark.parametrize("tool", list(_OPTIONAL_TOOL_ARGUMENTS))
@pytest.mark.anyio
async def test_unavailable_optional_source_can_be_explained(tool: str) -> None:
    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returned:
            yield {
                0: DeltaToolCall(
                    name=tool,
                    json_args=json.dumps(_OPTIONAL_TOOL_ARGUMENTS[tool]),
                )
            }
        else:
            assert "unavailable" in str(returned), returned
            assert "private storage detail" not in str(returned), returned
            yield "The requested source is unavailable. Please upload it again."

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="private storage detail")
        )
    ) as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Read the charter",
                    capability="b" * 64,
                    library_enabled=True,
                    web_search=tool == "web_search",
                )
            )
        ]
    assert chunks[-1].kind == "completed", chunks
    assert "private storage detail" not in "".join(
        chunk.content for chunk in chunks
    )
    warnings = [
        json.loads(chunk.content)
        for chunk in chunks
        if chunk.kind == "activity"
        and json.loads(chunk.content)["type"] == "warning"
    ]
    assert warnings == [
        {"type": "warning", "source": "web" if tool == "web_search" else tool}
    ]


@pytest.mark.anyio
async def test_deleted_historical_object_does_not_abort_the_next_question() -> (
    None
):
    downloads: list[str] = []

    def storage(request: httpx.Request) -> httpx.Response:
        downloads.append(request.url.path)
        return httpx.Response(404, text="private missing object detail")

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        assert "unavailable" in str(messages)
        assert "private missing object detail" not in str(messages)
        assert "signature=private" not in str(messages)
        yield "Please upload the missing source again."

    attachment = Attachment(
        kind="document",
        url="http://127.0.0.1:9000/missing?signature=private",
        media_type="application/pdf",
        trusted_storage=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(storage)
    ) as client:
        runtime = Runtime(
            settings(), client, model=FunctionModel(stream_function=response)
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="What does the charter say?",
                    capability="b" * 64,
                    attachments=[attachment],
                    history=[
                        HistoryMessage(
                            role="user",
                            content="Read this",
                            attachments=[attachment],
                        )
                    ],
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert downloads == ["/missing"]


def test_personal_and_project_instructions_are_bounded_and_separate_from_evidence() -> (
    None
):
    from galadril_scribe.contracts import RunRequest
    from galadril_scribe.instructions import generation_instructions
    from pydantic import ValidationError

    request = RunRequest(
        prompt="Hello",
        capability="a" * 64,
        user_instructions="Réponds en français.",
        project_instructions="Use a formal tone.",
    )
    instructions = generation_instructions(request, "Verify sources.")
    assert instructions is not None
    assert instructions.index("Réponds") < instructions.index("Use a formal")
    assert "Verify sources." in instructions
    assert "approval" in instructions
    for value in ["x" * 2001, "bad\x00"]:
        with pytest.raises(ValidationError):
            RunRequest(
                prompt="Hello", capability="a" * 64, user_instructions=value
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
