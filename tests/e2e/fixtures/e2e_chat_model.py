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
    question: str = Field(min_length=1, max_length=4096)


class CompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, JsonValue]] = Field(max_length=256)
    stream: bool = True


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
    last_user = next(
        (
            index
            for index in range(len(request.messages) - 1, -1, -1)
            if request.messages[index].get("role") == "user"
        ),
        None,
    )
    if last_user is None or not request.stream:
        raise HTTPException(422, "A streamed user request is required")
    prompt = request.messages[last_user].get("content")
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
        raise HTTPException(422, "The fixture requires a text query plan")
    plan = QueryPlan.model_validate_json(prompt)
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
        if not results:
            arguments = (
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
