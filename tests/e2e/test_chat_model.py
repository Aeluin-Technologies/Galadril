"""Checks the fixture against the production OpenAI agent adapter."""

import json

import httpx
import pytest
from e2e_chat_model import app
from galadril_scribe.contracts import RunRequest, Settings
from galadril_scribe.runtime import Runtime


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_native_agent_uses_tool_results_as_the_only_answer_oracle() -> (
    None
):
    operations: list[str] = []
    fixture = httpx.ASGITransport(app=app)
    results = {
        "search": {
            "evidence": [{"entity_id": "entity-a", "state": {"value": 17}}]
        },
        "graph": {
            "nodes": [{"id": "entity-a", "label": "Customer"}],
            "edges": [],
        },
        "causal": {"analyses": [{"summary": {"causal_links": 3}}]},
    }

    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model":
            return await fixture.handle_async_request(request)
        arguments = json.loads(request.content)
        operation = arguments["operation"]
        operations.append(operation)
        assert request.headers["authorization"] == "Bearer " + "a" * 64
        if operation == "search":
            assert arguments["entity_id"] == "entity-a"
        assert "tenant_id" not in arguments
        return httpx.Response(200, json=results[operation])

    settings = Settings(
        service_token="x" * 32,
        gateway_tools_url="http://gateway/internal/chat/tools",
        models={"fixture": {"model": "e2e", "base_url": "http://model/v1"}},
        default_model="fixture",
        max_output_tokens=8192,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(route)
    ) as client:
        runtime = Runtime(settings, client)
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt=json.dumps(
                        {
                            "entity_id": "entity-a",
                            "causal_entity_id": "entity-a",
                            "question": "Find evidence",
                        }
                    ),
                    capability="a" * 64,
                )
            )
        ]
    assert sorted(operations) == ["causal", "graph", "search"]
    assert chunks[-1].kind == "completed"
    assert json.loads("".join(chunk.content for chunk in chunks)) == results


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
