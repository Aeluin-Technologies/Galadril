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
            "models": {alias: models[alias] for alias in aliases},
            "default_model": next(iter(aliases), None),
            "openai_api_key": "cloud-secret",
        }
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
        assert names == {"search", "graph", "causal", "run_python"}
        assert "a" * 64 not in incoming.content.decode()
        if provider == "openai":
            assert payload["model"] == model_name
            assert "reasoning.encrypted_content" in payload.get("include", [])
            assert payload["reasoning"] == {"effort": effort or "medium"}
            assert payload["store"] is False
            assert payload["max_output_tokens"] == 1024
        else:
            assert "reasoning_effort" not in payload
            assert payload["max_completion_tokens"] == 1024
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
    assert "42" in "".join(chunk["content"] for chunk in chunks)
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
