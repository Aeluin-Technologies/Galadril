"""Verify provider isolation and agent tool parity at the HTTP protocol boundary."""

import asyncio
import json
import sys
from typing import Literal

import httpx
import pytest
from galadril_scribe.contracts import Settings
from galadril_scribe.runtime import Runtime, create_app
from galadril_scribe.sandbox import Sandbox
from pydantic import ValidationError


def configuration(*aliases: str) -> Settings:
    models = {
        "local": {
            "provider": "vllm",
            "model": "local-tools",
            "base_url": "http://vllm/v1",
            "api_key": "local-secret",
        },
        "cloud": {
            "provider": "openai",
            "model": "gpt-5.1",
            "reasoning_efforts": ["none", "low", "medium", "high"],
            "default_reasoning_effort": "medium",
        },
    }
    return Settings.model_validate(
        {
            "gateway_tools_url": "http://gateway/internal/chat/tools",
            "web_search_provider": "openai",
            "models": {alias: models[alias] for alias in aliases},
            "default_model": next(iter(aliases), None),
            "openai_api_key": "cloud-secret",
        }
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("remote", [True, False])
async def test_document_relations_use_service_key_and_strict_responses_schema(
    remote: bool,
) -> None:
    from galadril_scribe.retrieval_contracts import ExtractFacts, ExtractPassage

    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer service-key"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        assert body["store"] is False
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "id": "resp_relation",
                "object": "response",
                "created_at": 0,
                "model": "gpt-6-luna",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_relation",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"facts":[{"subject":"Alice","subject_type":"PERSON","predicate":"visits","object":"Paris","object_type":"LOCATION","evidence":"Alice visits Paris"}]}',
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "total_tokens": 30,
                },
            },
        )

    configured = Settings.model_validate(
        {
            "gateway_tools_url": "http://gateway/internal/chat/tools",
            "openai_api_key": "service-key",
            "document_remote_relations": remote,
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(configured, client)
        result = await runtime.documents.extract(
            ExtractFacts(
                passages=(
                    ExtractPassage(id="a" * 64, text="Alice visits Paris"),
                )
            )
        )
    assert len(calls) == int(remote)
    assert bool(result.passages[0].facts) == remote


@pytest.mark.anyio
async def test_openai_models_default_to_delegated_search_without_paid_fallback() -> (
    None
):
    from galadril_scribe.contracts import RunRequest
    from pydantic_ai.models.test import TestModel

    configured = configuration("cloud").model_dump()
    configured.pop("web_search_provider")
    calls: list[httpx.Request] = []

    def transport(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        assert incoming.url.host == "gateway"
        assert json.loads(incoming.content) == {
            "operation": "web",
            "query": "public decision",
        }
        return httpx.Response(503)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(
            Settings.model_validate(configured),
            client,
            model=TestModel(call_tools=[]),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Research",
                    capability="a" * 64,
                    web_query="public decision",
                )
            )
        ]
    assert len(calls) == 1
    assert chunks[-1].kind == "completed"
    assert any(
        chunk.kind == "activity"
        and json.loads(chunk.content).get("source") == "web"
        for chunk in chunks
    )


@pytest.mark.anyio
async def test_chatgpt_plan_is_disabled_by_default_and_never_uses_environment_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "unrelated-secret")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Unexpected provider call")
        )
    ) as outbound:
        runtime = Runtime(configuration("cloud"), outbound)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            response = await client.post(
                "/openai/accounts",
                json={"operation": "status", "owner": "a" * 64},
            )
            assert response.status_code == 200
            assert response.json()["enabled"] is False
            response = await client.post(
                "/openai/accounts",
                json={
                    "operation": "begin",
                    "owner": "a" * 64,
                    "redirect": "http://127.0.0.1:1455/auth/callback",
                },
            )
            assert response.status_code == 503
            assert "unrelated-secret" not in response.text


def test_chatgpt_configuration_requires_private_storage_and_an_explicit_encryption_key() -> (
    None
):
    values = configuration("cloud").model_dump()
    with pytest.raises(ValidationError):
        Settings.model_validate(values | {"chatgpt_enabled": True})
    with pytest.raises(ValidationError):
        Settings.model_validate(
            values
            | {
                "chatgpt_enabled": True,
                "chatgpt_encryption_key": "short",
                "chatgpt_credentials_path": "/private/accounts.sqlite",
            }
        )
    loaded = Settings.model_validate(
        values
        | {
            "chatgpt_enabled": True,
            "chatgpt_encryption_key": "aw" * 21 + "s=",
            "chatgpt_credentials_path": "/private/accounts.sqlite",
        }
    )
    assert loaded.chatgpt_enabled


def test_max_reasoning_effort_can_be_declared_for_supported_models() -> None:
    from galadril_scribe.contracts import RunRequest

    configured = Settings.model_validate(
        configuration("cloud").model_dump()
        | {
            "models": {
                "cloud": {
                    "provider": "openai",
                    "model": "gpt-6-astra",
                    "reasoning_efforts": ["max"],
                    "default_reasoning_effort": "max",
                }
            },
        }
    )
    incoming = RunRequest.model_validate(
        {"prompt": "Reason", "capability": "a" * 64, "reasoning_effort": "max"}
    )
    assert (
        configured.models["cloud"].default_reasoning_effort
        == incoming.reasoning_effort
    )


@pytest.mark.parametrize("alias", [None, "local", "cloud"])
def test_environment_allows_disabled_providers_without_unused_credentials(
    alias: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = configuration(*((alias,) if alias else ()))
    monkeypatch.setenv("SCRIBE_GATEWAY_TOOLS_URL", configured.gateway_tools_url)
    monkeypatch.setenv(
        "SCRIBE_MODELS",
        json.dumps(
            {
                name: {
                    "provider": model.provider,
                    "model": model.model,
                    **({"base_url": model.base_url} if model.base_url else {}),
                }
                for name, model in configured.models.items()
            }
        ),
    )
    monkeypatch.setenv("SCRIBE_DEFAULT_MODEL", alias or "")
    monkeypatch.setenv(
        "SCRIBE_OPENAI_API_KEY", "cloud-secret" if alias == "cloud" else ""
    )
    loaded = Settings()
    assert loaded.default_model == alias
    assert (loaded.openai_api_key is None) == (alias != "cloud")


@pytest.mark.parametrize(
    "aliases", [(), ("local",), ("cloud",), ("local", "cloud")]
)
@pytest.mark.anyio
async def test_catalog_lists_only_configured_models_without_contacting_providers(
    aliases: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://attacker/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")

    def unexpected(incoming: httpx.Request) -> httpx.Response:
        pytest.fail(f"Catalog contacted {incoming.url.host}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected)
    ) as outbound:
        runtime = Runtime(configuration(*aliases), outbound)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            for _ in range(2):
                response = await client.get("/models")
                assert response.status_code == 200
                catalog = response.json()
                assert catalog["default_model"] == next(iter(aliases), None)
                assert [model["alias"] for model in catalog["models"]] == list(
                    aliases
                )
                for model in catalog["models"]:
                    assert set(model) == {
                        "alias",
                        "provider",
                        "model",
                        "reasoning_efforts",
                        "default_reasoning_effort",
                    }
                    assert model["reasoning_efforts"] == (
                        ["none", "low", "medium", "high"]
                        if model["alias"] == "cloud"
                        else []
                    )
                assert "secret" not in response.text
                assert "base_url" not in response.text


@pytest.mark.parametrize(
    "changes",
    [
        {"openai_api_key": None},
        {"openai_api_key": ""},
        {"default_model": "missing"},
        {"models": {"": {"model": "local", "base_url": "http://vllm/v1"}}},
        {"models": {"local": {"model": "local"}}},
        {
            "models": {
                "local": {"model": "local", "base_url": "file:///tmp/model"}
            }
        },
        {
            "models": {
                "cloud": {
                    "provider": "openai",
                    "model": "gpt-5.1",
                    "base_url": "http://attacker/v1",
                }
            }
        },
        {
            "models": {
                "cloud": {
                    "provider": "openai",
                    "model": "gpt-5.1",
                    "reasoning_efforts": ["high"],
                    "default_reasoning_effort": "low",
                }
            }
        },
        {
            "models": {
                "cloud": {
                    "provider": "openai",
                    "model": "gpt-5.1",
                    "reasoning_efforts": ["high", "high"],
                }
            }
        },
    ],
)
def test_invalid_provider_configuration_is_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(configuration("cloud").model_dump() | changes)


@pytest.mark.anyio
async def test_disabled_runtime_and_invalid_reasoning_fail_before_admission() -> (
    None
):
    for aliases, status, extra in [
        ((), 503, {}),
        (("local", "cloud"), 422, {"model_alias": "missing"}),
        (
            ("local", "cloud"),
            422,
            {"model_alias": "local", "reasoning_effort": "high"},
        ),
        (
            ("local", "cloud"),
            422,
            {"model_alias": "cloud", "reasoning_effort": "xhigh"},
        ),
    ]:
        async with httpx.AsyncClient() as outbound:
            runtime = Runtime(configuration(*aliases), outbound)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(runtime)),
                base_url="http://scribe",
            ) as client:
                response = await client.post(
                    "/runs",
                    json={
                        "prompt": "private-prompt",
                        "capability": "a" * 64,
                        **extra,
                    },
                )
                assert response.status_code == status
                assert "private-prompt" not in response.text
                assert runtime.active == 0


def event_stream(events: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(f"data: {json.dumps(event)}\n\n" for event in events),
    )


@pytest.mark.anyio
async def test_signed_private_pdf_preserves_the_s3_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_scribe.contracts import RunRequest

    downloads: list[str] = []
    documents: list[dict[str, object]] = []

    def transport(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "127.0.0.1":
            downloads.append(incoming.headers["host"])
            if incoming.headers["host"] != "127.0.0.1:9000":
                return httpx.Response(403, text="SignatureDoesNotMatch")
            assert "authorization" not in incoming.headers
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.7\nprivate document",
            )
        payload = json.loads(incoming.content)
        documents.extend(
            part
            for message in payload["input"]
            for part in message.get("content", [])
            if isinstance(part, dict) and part.get("type") == "input_file"
        )
        return inference_response("openai", final=True)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(configuration("cloud"), client)
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest.model_validate(
                    {
                        "prompt": "Read the attached PDF",
                        "capability": "a" * 64,
                        "attachments": [
                            {
                                "kind": "document",
                                "url": "http://127.0.0.1:9000/lake/opaque?X-Amz-Signature=fixture",
                                "media_type": "application/pdf",
                                "trusted_storage": True,
                            }
                        ],
                    }
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert downloads == ["127.0.0.1:9000"]
    assert len(documents) == 1
    assert str(documents[0]["file_data"]).startswith(
        "data:application/pdf;base64,"
    )
    assert "Signature" not in str(documents[0])


def inference_response(provider: str, final: bool) -> httpx.Response:
    calls = [
        ("search", {"question": "evidence", "limit": 1}),
        ("graph", {"entity_id": "authorized", "depth": 1}),
        ("causal", {"entity_id": "authorized"}),
        ("run_python", {"code": "6 * 7"}),
    ]
    if provider == "vllm":
        delta: dict[str, object] = (
            {"content": "answer: 42"}
            if final
            else {
                "tool_calls": [
                    {
                        "index": index,
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                    for index, (name, arguments) in enumerate(calls)
                ]
            }
        )
        return event_stream(
            [
                {
                    "id": "chat_fixture",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "local-tools",
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": "stop" if final else "tool_calls",
                        }
                    ],
                }
            ]
        )
    response: dict[str, object] = {
        "id": "resp_final" if final else "resp_tools",
        "object": "response",
        "created_at": 1,
        "model": "gpt-5.1",
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
    }
    events: list[dict[str, object]] = [
        {"type": "response.created", "response": response}
    ]
    if final:
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": "msg_final",
                "output_index": 0,
                "content_index": 0,
                "delta": "answer: 42",
            }
        )
    else:
        events.append(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "reasoning",
                    "id": "rs_fixture",
                    "summary": [],
                    "encrypted_content": "private-encrypted-reasoning",
                },
            }
        )
        for index, (name, arguments) in enumerate(calls):
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {
                        "type": "function_call",
                        "id": f"fc_{index}",
                        "call_id": f"call_{index}",
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            )
    events.append({"type": "response.completed", "response": response})
    return event_stream(events)


@pytest.mark.parametrize(
    "provider,alias,effort",
    [
        ("openai", "cloud", "high"),
        ("openai", "cloud", None),
        ("vllm", "local", None),
    ],
)
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("model_name", ["gpt-5.1", "gpt-6.1-sol"])
@pytest.mark.anyio
async def test_real_provider_protocol_runs_identical_tools_and_isolates_credentials(
    provider: Literal["openai", "vllm"],
    alias: str,
    effort: str | None,
    mixed: bool,
    model_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_generation(event: str, **metadata: object) -> None:
        raise AssertionError(event) from sys.exception()

    monkeypatch.setattr(
        "galadril_scribe.runtime.logger.warning", fail_generation
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "http://attacker/v1")
    calls: list[httpx.Request] = []
    gateway_calls: list[httpx.Request] = []

    def transport(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "gateway":
            gateway_calls.append(incoming)
            assert incoming.headers["authorization"] == "Bearer " + "a" * 64
            return httpx.Response(200, json={"id": "authorized"})
        calls.append(incoming)
        assert incoming.url.host == (
            "api.openai.com" if provider == "openai" else "vllm"
        )
        assert incoming.url.path == (
            "/v1/responses" if provider == "openai" else "/v1/chat/completions"
        )
        assert incoming.headers["authorization"] == (
            "Bearer cloud-secret"
            if provider == "openai"
            else "Bearer local-secret"
        )
        payload = json.loads(incoming.content)
        names = {
            tool["name"] if provider == "openai" else tool["function"]["name"]
            for tool in payload["tools"]
        }
        assert names == {
            "search",
            "graph",
            "causal",
            "run_python",
            "write_plan",
            "read_plan",
            "update_task_status",
        }
        assert "a" * 64 not in incoming.content.decode()
        if provider == "openai":
            assert payload["model"] == model_name
            assert "reasoning.encrypted_content" in payload.get("include", [])
            assert payload["reasoning"]["effort"] == (effort or "medium")
            if model_name.startswith("gpt-6"):
                assert payload["reasoning"]["context"] == "all_turns"
            assert payload["store"] is False
            assert payload["max_output_tokens"] == configured.max_output_tokens
        else:
            assert "reasoning_effort" not in payload
            assert (
                payload["max_completion_tokens"] == configured.max_output_tokens
            )
        return inference_response(provider, final=len(calls) == 2)

    configured = configuration(*(("local", "cloud") if mixed else (alias,)))
    if provider == "openai":
        configured = Settings.model_validate(
            configured.model_dump()
            | {
                "models": {
                    name: config.model_dump()
                    | ({"model": model_name} if name == "cloud" else {})
                    for name, config in configured.models.items()
                },
            }
        )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(transport)) as outbound,
        Sandbox() as sandbox,
    ):
        runtime = Runtime(configured, outbound, sandbox=sandbox)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            payload: dict[str, object] = {
                "prompt": "Find evidence and calculate",
                "capability": "a" * 64,
                "model_alias": alias,
            }
            if effort is not None:
                payload["reasoning_effort"] = effort
            response = await client.post("/runs", json=payload)
    assert response.status_code == 200
    chunks = [json.loads(line) for line in response.text.splitlines()]
    assert chunks[-1]["kind"] == "completed"
    assert "42" in "".join(
        chunk["content"] for chunk in chunks if chunk["kind"] == "content"
    )
    assert "private-encrypted-reasoning" not in response.text
    assert len(calls) == 2
    if provider == "openai":
        replay = json.loads(calls[1].content)["input"]
        assert any(
            item.get("type") == "reasoning"
            and item.get("encrypted_content") == "private-encrypted-reasoning"
            for item in replay
        )
        outputs = [
            item
            for item in json.loads(calls[1].content)["input"]
            if item.get("type") == "function_call_output"
        ]
        assert len(outputs) == 4
        assert any(item["output"] == "42" for item in outputs)
    assert {
        json.loads(call.content)["operation"] for call in gateway_calls
    } == {"search", "graph", "causal"}
    assert all(
        "capability" not in json.loads(call.content) for call in gateway_calls
    )


@pytest.mark.anyio
async def test_both_providers_can_generate_concurrently() -> None:
    entered: set[str] = set()
    ready = asyncio.Event()
    calls: dict[str, int] = {}

    async def transport(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "gateway":
            return httpx.Response(200, json={"id": "authorized"})
        provider = "openai" if incoming.url.host == "api.openai.com" else "vllm"
        entered.add(provider)
        if len(entered) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=3)
        calls[provider] = calls.get(provider, 0) + 1
        return inference_response(provider, final=calls[provider] == 2)

    configured = Settings.model_validate(
        configuration("local", "cloud").model_dump()
        | {"max_concurrent_runs": 2}
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(transport)) as outbound,
        Sandbox() as sandbox,
    ):
        runtime = Runtime(configured, outbound, sandbox=sandbox)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/runs",
                        json={
                            "prompt": "Find and calculate",
                            "capability": "a" * 64,
                            "model_alias": alias,
                        },
                    )
                    for alias in ("local", "cloud")
                ]
            )
    assert entered == {"openai", "vllm"}
    assert calls == {"openai": 2, "vllm": 2}
    assert all(
        json.loads(response.text.splitlines()[-1])["kind"] == "completed"
        for response in responses
    )
    assert runtime.active == 0


@pytest.mark.anyio
async def test_provider_failure_never_falls_back_to_another_provider() -> None:
    contacted: set[str | None] = set()

    def transport(incoming: httpx.Request) -> httpx.Response:
        contacted.add(incoming.url.host)
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "private-provider-details",
                    "type": "permission_error",
                }
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(configuration("local", "cloud"), client)
        from galadril_scribe.contracts import RunRequest

        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest.model_validate(
                    {
                        "prompt": "private-prompt",
                        "capability": "a" * 64,
                        "model_alias": "cloud",
                    }
                )
            )
        ]
    assert contacted == {"api.openai.com"}
    assert chunks[-1].kind == "failed"
    assert "private-provider-details" not in "".join(
        chunk.content for chunk in chunks
    )


@pytest.mark.anyio
async def test_local_reasoning_and_per_model_keys_are_forwarded_explicitly() -> (
    None
):
    configured = Settings.model_validate(
        configuration("local", "cloud").model_dump()
        | {
            "openai_api_key": None,
            "models": {
                "cloud": {
                    "provider": "openai",
                    "model": "gpt-5.1",
                    "api_key": "per-model-secret",
                },
                "local": {
                    "model": "local-tools",
                    "base_url": "http://vllm/v1",
                    "reasoning_efforts": ["low", "high"],
                    "default_reasoning_effort": "low",
                },
            },
        }
    )
    calls: list[httpx.Request] = []

    def transport(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        provider = "openai" if incoming.url.host == "api.openai.com" else "vllm"
        return inference_response(provider, final=True)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as outbound:
        runtime = Runtime(configured, outbound)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as client:
            for alias in ("cloud", "local"):
                response = await client.post(
                    "/runs",
                    json={
                        "prompt": "Calculate",
                        "capability": "a" * 64,
                        "model_alias": alias,
                    },
                )
                assert (
                    json.loads(response.text.splitlines()[-1])["kind"]
                    == "completed"
                )
    assert len(calls) == 2
    assert calls[0].headers["authorization"] == "Bearer per-model-secret"
    assert "reasoning" not in json.loads(calls[0].content)
    assert calls[1].headers["authorization"] == "Bearer local"
    assert json.loads(calls[1].content)["reasoning_effort"] == "low"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))


def test_default_output_budget_leaves_room_for_medium_reasoning() -> None:
    assert configuration("cloud").max_output_tokens == 4096


@pytest.mark.anyio
async def test_native_web_search_uses_only_the_explicit_public_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_scribe.contracts import RunRequest

    def fail_generation(event: str, **metadata: object) -> None:
        raise AssertionError(event) from sys.exception()

    monkeypatch.setattr(
        "galadril_scribe.runtime.logger.warning", fail_generation
    )

    calls: list[dict[str, object]] = []

    def transport(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url.host == "api.openai.com"
        payload = json.loads(incoming.content)
        calls.append(payload)
        if len(calls) == 1:
            assert payload["max_tool_calls"] > 1
            assert payload["tool_choice"] == "required"
            assert payload["tools"][0]["type"] == "web_search"
            assert len(payload["tools"]) == 1
            assert "public weather evidence" in incoming.content.decode()
            for private in ("private question", "private history", "a" * 64):
                assert private not in incoming.content.decode()
            assert payload["store"] is False
            return httpx.Response(
                200,
                json={
                    "id": "resp_web",
                    "object": "response",
                    "created_at": 1,
                    "model": "gpt-5.1",
                    "status": "completed",
                    "output": [
                        {
                            "id": "web_message",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Public evidence [Source](https://example.com/weather)",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "start_index": 0,
                                            "end_index": 15,
                                            "title": "Official source",
                                            "url": "https://example.com/official",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 10,
                        "total_tokens": 20,
                    },
                },
            )
        assert "Public evidence" in incoming.content.decode()
        assert "https://example.com/official" in incoming.content.decode()
        assert all(tool["type"] != "web_search" for tool in payload["tools"])
        return inference_response("openai", final=True)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(configuration("cloud"), client)
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest.model_validate(
                    {
                        "prompt": "private question",
                        "capability": "a" * 64,
                        "web_query": "public weather evidence",
                        "history": [
                            {"role": "user", "content": "private history"}
                        ],
                    }
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert len(calls) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_agent_selected_native_research_is_isolated_and_can_visit_multiple_sources(
    legacy: bool,
) -> None:
    from collections.abc import AsyncIterator

    from galadril_scribe.contracts import RunRequest
    from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    # Native browsing consumes up to 4 units per search. Give this scenario
    # exactly two successful native searches, followed by an exhausted-budget
    # tool result. Do not rely on the application-wide default (currently 32).
    configured = Settings.model_validate(
        configuration("cloud").model_dump() | {"max_tool_calls": 8}
    )
    requests: list[dict[str, object]] = []
    request_bodies: list[bytes] = []
    final_histories: list[str] = []

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        returned = sum(
            isinstance(part, ToolReturnPart)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if returned < 3:
            yield {
                0: DeltaToolCall(
                    name="web_search",
                    json_args=json.dumps(
                        {"query": f"public source {returned}"}
                    ),
                )
            }
        else:
            # Assert on the completed model history outside this callback;
            # Runtime intentionally converts callback exceptions into 'failed'.
            final_histories.append(str(messages))
            yield "Answer citing [source](https://example.com/source)."

    def transport(incoming: httpx.Request) -> httpx.Response:
        requests.append(json.loads(incoming.content))
        request_bodies.append(incoming.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_public",
                "object": "response",
                "created_at": 1,
                "model": "gpt-5.1",
                "status": "completed",
                "output": [
                    {
                        "id": "message",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Public research result",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "start_index": 0,
                                        "end_index": 6,
                                        "title": "Source",
                                        "url": "https://example.com/source",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(configured, client)
        with runtime.agent.override(
            model=FunctionModel(stream_function=response)
        ):
            chunks = [
                chunk
                async for chunk in runtime.stream(
                    RunRequest(
                        prompt="private question",
                        capability="a" * 64,
                        web_search=True,
                        web_query="public initial" if legacy else None,
                        history=[
                            {"role": "user", "content": "private history"}
                        ],
                    )
                )
            ]

    expected_queries = (
        ["public initial", "public source 0"]
        if legacy
        else ["public source 0", "public source 1"]
    )
    assert [payload["input"] for payload in requests] == expected_queries
    # Both successful native searches have a budget of 4. The next call to
    # web_search is answered locally with 'budget exhausted', without HTTP.
    assert [payload["max_tool_calls"] for payload in requests] == [4, 4]
    for payload, body in zip(requests, request_bodies, strict=True):
        assert payload["tools"] == [
            {"type": "web_search", "search_context_size": "medium"}
        ]
        assert payload["reasoning"] == {"effort": "medium"}
        assert payload["tool_choice"] == "required"
        assert payload["store"] is False
        for secret in ("private question", "private history", "a" * 64):
            assert secret.encode() not in body, (
                "Private data leaked to web research"
            )

    assert len(final_histories) == 1, (
        "The model never received all tool returns"
    )
    assert "https://example.com/source" in final_histories[0]
    assert "Public web research budget exhausted" in final_histories[0]
    assert chunks and chunks[-1].kind == "completed", [
        (chunk.kind, chunk.content[:200]) for chunk in chunks[-5:]
    ]


@pytest.mark.anyio
async def test_public_research_survives_an_unavailable_internal_search() -> (
    None
):
    from collections.abc import AsyncIterator

    from galadril_scribe.contracts import RunRequest
    from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    operations: list[str] = []

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
                    name="search",
                    json_args=json.dumps(
                        {"question": "Who is the public researcher?"}
                    ),
                )
            }
        elif len(returned) == 1:
            assert "unavailable" in str(returned)
            assert "private SQL detail" not in str(returned)
            yield {
                0: DeltaToolCall(
                    name="web_search",
                    json_args=json.dumps(
                        {"query": "public researcher biography"}
                    ),
                )
            }
        else:
            assert "Public biography" in str(returned)
            yield "Public biography [Source](https://example.com/biography)."

    def transport(incoming: httpx.Request) -> httpx.Response:
        if incoming.url.host == "gateway":
            operations.append("search")
            return httpx.Response(403, text="private SQL detail")
        operations.append("web")
        payload = json.loads(incoming.content)
        assert payload["input"] == "public researcher biography"
        assert "private SQL detail" not in incoming.content.decode()
        return httpx.Response(
            200,
            json={
                "id": "resp_public",
                "object": "response",
                "created_at": 1,
                "model": "gpt-5.1",
                "status": "completed",
                "output": [
                    {
                        "id": "message",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Public biography",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(configuration("cloud"), client)
        with runtime.agent.override(
            model=FunctionModel(stream_function=response)
        ):
            chunks = [
                chunk
                async for chunk in runtime.stream(
                    RunRequest(
                        prompt="Find this public researcher",
                        capability="a" * 64,
                        web_search=True,
                    )
                )
            ]
    assert chunks[-1].kind == "completed"
    assert operations == ["search", "web"]
