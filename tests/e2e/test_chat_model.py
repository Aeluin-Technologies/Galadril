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
    assert (
        json.loads(
            "".join(
                chunk.content for chunk in chunks if chunk.kind == "content"
            )
        )
        == results
    )


@pytest.mark.anyio
async def test_shared_project_fixture_can_use_tools_without_private_graph_evidence() -> (
    None
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "e2e",
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "entity_id": "entity-a",
                                "causal_entity_id": "entity-a",
                                "question": "Calculate",
                                "calculator_only": True,
                            }
                        ),
                    }
                ],
            },
        )
    assert response.status_code == 200
    assert '"name": "run_python"' in response.text
    assert '"name": "graph"' not in response.text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))


@pytest.mark.anyio
async def test_document_search_then_read_uses_the_returned_document() -> None:
    operations: list[str] = []
    fixture = httpx.ASGITransport(app=app)

    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "model":
            return await fixture.handle_async_request(request)
        arguments = json.loads(request.content)
        operations.append(arguments["operation"])
        if arguments["operation"] == "document_search":
            assert arguments["query"] == "EVdiénce"
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "sourceId": "a" * 32,
                            "sourceChecksum": "b" * 64,
                            "unitId": "c" * 64,
                            "filename": "Evidence.txt",
                            "text": "Evidence excerpt",
                            "page": None,
                            "similarity": 0.8,
                            "relevance": 0.9,
                        }
                    ],
                    "warnings": [],
                },
            )
        assert arguments == {"operation": "read_file", "file_id": "a" * 32}
        return httpx.Response(
            200,
            json={
                "id": "a" * 32,
                "name": "Evidence.txt",
                "attachment": {
                    "kind": "text",
                    "text": "The secret source says forty-two.",
                },
            },
        )

    configured = Settings(
        gateway_tools_url="http://gateway/internal/chat/tools",
        models={"fixture": {"model": "e2e", "base_url": "http://model/v1"}},
        default_model="fixture",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(route)
    ) as client:
        runtime = Runtime(configured, client)
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt=json.dumps(
                        {
                            "entity_id": "entity-a",
                            "causal_entity_id": "entity-a",
                            "question": "EVdiénce",
                            "library": True,
                        }
                    ),
                    capability="a" * 64,
                    library_enabled=True,
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert operations == ["document_search", "read_file"]
    assert "forty-two" in "".join(chunk.content for chunk in chunks)
