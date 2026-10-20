"""Prove plan-to-API failover cannot replay tools or publish failed partial output."""

import json

import httpx
import pytest
from galadril_scribe.chatgpt_accounts import AccessCredential
from galadril_scribe.chatgpt_model import ChatGPTModel
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.profiles.openai import openai_model_profile


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Accounts:
    __slots__ = ("exhausted_calls", "available", "supported")

    def __init__(self) -> None:
        self.exhausted_calls = 0
        self.available = True
        self.supported = ("gpt-6.1-sol",)

    async def access(self, owner: str) -> AccessCredential | None:
        assert owner == "a" * 64
        return (
            AccessCredential(
                token=SecretStr("plan-secret"), registration="fixture"
            )
            if self.available
            else None
        )

    async def models(self, owner: str) -> tuple[str, ...]:
        return self.supported

    async def exhausted(self, owner: str, registration: str) -> None:
        assert owner == "a" * 64 and registration == "fixture"
        self.exhausted_calls += 1
        self.available = False


def stream(
    *, call: bool = False, failed: bool = False, incomplete: bool = False
) -> httpx.Response:
    response: dict[str, object] = {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6.1-sol",
        "status": "in_progress",
        "output": [],
    }
    events: list[dict[str, object]] = [
        {"type": "response.created", "response": response.copy()}
    ]
    if call:
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_fixture",
                    "call_id": "call_fixture",
                    "name": "calculate",
                    "arguments": "{}",
                },
            }
        )
    else:
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": "msg_fixture",
                "output_index": 0,
                "content_index": 0,
                "delta": "unsafe partial" if failed else "Answer: 42",
            }
        )
    if not incomplete:
        response["status"] = "failed" if failed else "completed"
        if failed:
            response["error"] = {
                "code": "subscription_sharing_usage_limit_exceeded",
                "message": "Limit reached",
            }
        events.append(
            {
                "type": "response.failed" if failed else "response.completed",
                "response": response,
            }
        )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(
            "data: " + json.dumps(event) + "\n\n" for event in events
        ),
    )


@pytest.mark.anyio
async def test_quota_failure_midstream_falls_back_without_replaying_completed_tools() -> (
    None
):
    accounts = Accounts()
    activity: list[str] = []
    calls: list[str] = []
    api_history: list[dict[str, object]] = []
    calculations = 0

    async def emit(content: str) -> None:
        activity.append(content)

    def transport(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        body = json.loads(request.content)
        token = request.headers["authorization"]
        calls.append(token)
        assert body["stream"] is True and body["store"] is False
        if token == "Bearer plan-secret":
            assert "max_output_tokens" not in body
            assert "previous_response_id" not in body
            assert not any(
                item.get("role") == "system" for item in body["input"]
            )
            declarations = [
                item
                for item in body["input"]
                if item.get("type") == "additional_tools"
            ]
            assert declarations and declarations[0]["role"] == "developer"
            assert any(
                tool["name"] == "calculate" for tool in declarations[0]["tools"]
            )
            return stream(call=len(calls) == 1, failed=len(calls) > 1)
        assert token == "Bearer api-secret"
        assert body["max_output_tokens"] == 2048
        api_history.extend(body["input"])
        return stream()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        model = ChatGPTModel(
            "gpt-6.1-sol",
            accounts=accounts,
            owner="a" * 64,
            api_key=SecretStr("api-secret"),
            client=client,
            emit=emit,
            profile=openai_model_profile("gpt-6.1-sol"),
        )
        agent = Agent(model, instructions="Use calculate before answering.")

        @agent.tool_plain
        def calculate() -> int:
            nonlocal calculations
            calculations += 1
            return 42

        result = await agent.run(
            "Calculate", model_settings={"max_tokens": 2048}
        )
        assert result.output == "Answer: 42"
        assert calculations == 1
        assert accounts.exhausted_calls == 1
        assert calls == [
            "Bearer plan-secret",
            "Bearer plan-secret",
            "Bearer api-secret",
        ]
        assert any(
            item.get("type") == "function_call_output"
            and item.get("output") == "42"
            for item in api_history
        )
        assert "unsafe partial" not in result.output
        assert any(
            json.loads(item).get("reason") == "quota" for item in activity
        )


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403])
async def test_policy_and_authentication_denials_do_not_trigger_paid_fallback(
    status: int,
) -> None:
    calls = 0

    async def emit(_: str) -> None:
        return None

    def transport(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={"error": {"code": "permission_denied", "message": "Denied"}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        model = ChatGPTModel(
            "gpt-6.1-sol",
            accounts=Accounts(),
            owner="a" * 64,
            api_key=SecretStr("api-secret"),
            client=client,
            emit=emit,
            profile=openai_model_profile("gpt-6.1-sol"),
        )
        with pytest.raises(ModelAPIError):
            await Agent(model).run("Hello")
    assert calls == 1


@pytest.mark.anyio
async def test_interrupted_stream_is_not_a_success_and_no_api_key_cannot_bill() -> (
    None
):
    async def emit(_: str) -> None:
        return None

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: stream(incomplete=True))
    ) as client:
        model = ChatGPTModel(
            "gpt-6.1-sol",
            accounts=Accounts(),
            owner="a" * 64,
            api_key=None,
            client=client,
            emit=emit,
            profile=openai_model_profile("gpt-6.1-sol"),
        )
        with pytest.raises(ModelAPIError):
            await Agent(model).run("Hello")


@pytest.mark.anyio
async def test_model_not_in_account_catalog_uses_configured_api_without_trying_the_plan() -> (
    None
):
    accounts = Accounts()
    accounts.supported = ()
    activity: list[str] = []

    async def emit(value: str) -> None:
        activity.append(value)

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer api-secret"
        return stream()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        model = ChatGPTModel(
            "gpt-6.1-sol",
            accounts=accounts,
            owner="a" * 64,
            api_key=SecretStr("api-secret"),
            client=client,
            emit=emit,
            profile=openai_model_profile("gpt-6.1-sol"),
        )
        assert (await Agent(model).run("Hello")).output == "Answer: 42"
    assert any(
        json.loads(item).get("reason") == "model_unavailable"
        for item in activity
    )


@pytest.mark.anyio
async def test_rotating_provider_token_is_registered_with_the_run_guard_before_inference() -> (
    None
):
    from galadril_scribe.builtin import SecretPolicy

    policy = SecretPolicy(("api-secret",))

    async def emit(_: str) -> None:
        return None

    def transport(request: httpx.Request) -> httpx.Response:
        assert policy.public_text("plan-secret") == "[REDACTED]"
        return stream()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        model = ChatGPTModel(
            "gpt-6.1-sol",
            accounts=Accounts(),
            owner="a" * 64,
            api_key=None,
            client=client,
            emit=emit,
            profile=openai_model_profile("gpt-6.1-sol"),
            protect=policy.register_secret,
        )
        assert (await Agent(model).run("Hello")).output == "Answer: 42"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
