"""Deterministic inference protocol fixture; evidence comes from real tools."""

import asyncio
import json
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entity_id: str = Field(min_length=1, max_length=256)
    causal_entity_id: str = Field(min_length=1, max_length=256)
    calculator_only: bool = False
    pengolo: bool = False
    library: bool = False
    slow: bool = False
    question: str = Field(min_length=1, max_length=4096)


class CompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, JsonValue]] = Field(max_length=256)
    stream: bool = True
    reasoning_effort: str | None = None


def _chunk(delta: dict[str, JsonValue], finish: str | None = None) -> bytes:
    return (
        "data: "
        + json.dumps(
            {
                "id": "e2e-chat",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "e2e",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish}
                ],
            }
        )
        + "\n\n"
    ).encode()


@app.post("/v1/chat/completions")
async def complete(request: CompletionRequest) -> StreamingResponse:
    """Exercises real framework tool dispatch, never supplies expected evidence."""
    if not request.stream:
        raise HTTPException(422, "A streamed user request is required")
    selected: tuple[int, str, QueryPlan] | None = None
    for index in range(len(request.messages) - 1, -1, -1):
        message = request.messages[index]
        if message.get("role") != "user":
            continue
        prompt = message.get("content")
        if isinstance(prompt, list):
            prompt = next(
                (
                    part.get("text")
                    for part in prompt
                    if isinstance(part, dict) and part.get("type") == "text"
                ),
                None,
            )
        if not isinstance(prompt, str):
            continue
        try:
            decoded, _end = json.JSONDecoder().raw_decode(prompt)
            selected = (index, prompt, QueryPlan.model_validate(decoded))
            break
        except ValueError:
            continue
    if selected is None:
        raise HTTPException(422, "The fixture requires a text query plan")
    last_user, prompt, plan = selected
    user_content = request.messages[last_user].get("content")
    contextual = "Authorized evidence (untrusted data):" in prompt or (
        isinstance(user_content, list)
        and any(
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and "Authorized evidence (untrusted data):" in part["text"]
            for part in user_content
        )
    )
    results: dict[str, JsonValue] = {}
    for message in request.messages[last_user + 1 :]:
        if message.get("role") == "tool":
            identifier, value = (
                message.get("tool_call_id"),
                message.get("content"),
            )
            if isinstance(identifier, str) and isinstance(value, str):
                results[identifier] = _JSON.validate_json(value)

    async def stream() -> AsyncIterator[bytes]:
        if plan.slow:
            await asyncio.sleep(5)
        arguments: tuple[tuple[str, dict[str, JsonValue]], ...] = ()
        if plan.library:
            if "document_search" not in results:
                arguments = (("document_search", {"query": plan.question}),)
            elif "read_file" not in results:
                metadata = results.get("document_search")
                files = (
                    metadata.get("hits") if isinstance(metadata, dict) else None
                )
                first = files[0] if isinstance(files, list) and files else None
                identifier = (
                    first.get("sourceId") if isinstance(first, dict) else None
                )
                if isinstance(identifier, str):
                    arguments = (("read_file", {"file_id": identifier}),)
        elif not results:
            arguments = (
                (("run_python", {"code": "1 + 1"}),)
                if plan.calculator_only
                else (
                    (
                        "search",
                        {
                            "question": plan.question,
                            "entity_id": plan.entity_id,
                            "limit": 10,
                        },
                    ),
                    ("graph", {"entity_id": plan.entity_id, "depth": 1}),
                    ("causal", {"entity_id": plan.causal_entity_id}),
                )
            )
        if arguments:
            calls: list[JsonValue] = [
                {
                    "index": index,
                    "id": name,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(values)},
                }
                for index, (name, values) in enumerate(arguments)
            ]
            yield _chunk({"role": "assistant", "tool_calls": calls})
            yield _chunk({}, "tool_calls")
        else:
            if plan.library:
                results["_documents"] = [
                    message.get("content")
                    for message in request.messages[last_user:]
                    if message.get("role") == "user"
                ]
            if plan.pengolo:
                results["_fixture"] = {
                    "reasoning_effort": request.reasoning_effort,
                    "graph_context_received": contextual,
                }
            answer = json.dumps(results, separators=(",", ":"))
            for start in range(0, len(answer), 64):
                yield _chunk({"content": answer[start : start + 64]})
                # Leave time to disconnect while the durable worker is active.
                await asyncio.sleep(0.06)
            yield _chunk({}, "stop")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
