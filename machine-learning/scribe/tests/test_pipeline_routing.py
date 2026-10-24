"""Native routing shares Scribe models and bounds failures without tool access."""

import asyncio

import httpx
import pytest
from galadril_scribe.contracts import Settings
from galadril_scribe.runtime import Runtime, create_app
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel


def runtime(client: httpx.AsyncClient) -> Runtime:
    settings = Settings(
        gateway_tools_url="http://gateway/internal/chat/tools",
        models={
            "local": {"model": "local", "base_url": "http://local/v1"},
            "api": {
                "provider": "openai",
                "model": "remote",
                "api_key": "test-only",
            },
        },
        default_model="local",
    )
    return Runtime(
        settings,
        client,
        model=TestModel(custom_output_args={"branch": "legal"}),
    )


def payload(**updates: object) -> dict[str, object]:
    return {
        "text": "Document to classify",
        "labels": ["legal", "general"],
        "instructions": "Choose a branch",
        "model_alias": "local",
        **updates,
    }


def request(service: Runtime, body: dict[str, object]) -> httpx.Response:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://scribe",
        ) as api:
            return await api.post("/pipeline/route", json=body)

    return asyncio.run(run())


@pytest.mark.parametrize("alias", ["local", "api"])
def test_pipeline_routing_uses_the_same_models_as_scribe(alias: str) -> None:
    service = runtime(httpx.AsyncClient())
    result = request(service, payload(model_alias=alias))
    assert result.status_code == 200
    assert result.json() == {
        "branch": "legal",
        "model_alias": alias,
        "fallback": False,
    }
    assert service.active == 0


def test_removed_model_falls_back_to_a_remaining_scribe_model() -> None:
    service = runtime(httpx.AsyncClient())
    service.models.pop("local")
    service.settings.models.pop("local")
    result = request(service, payload())
    assert result.status_code == 200
    assert result.json()["model_alias"] == "api"
    assert result.json()["fallback"] is True
    assert service.active == 0


@pytest.mark.parametrize("status", [404, 410, 429, 503])
def test_provider_unavailability_uses_the_configured_fallback(
    status: int,
) -> None:
    service = runtime(httpx.AsyncClient())
    calls: list[str] = []

    def unavailable(messages: object, info: object) -> object:
        calls.append("local")
        raise ModelHTTPError(
            status, "removed-model", body={"error": "private-provider-detail"}
        )

    service.models["local"] = FunctionModel(unavailable)
    result = request(service, payload(fallback_models=["api"]))
    assert result.status_code == 200
    assert result.json()["model_alias"] == "api"
    assert calls == ["local"]
    assert "private-provider-detail" not in result.text


def test_no_remaining_model_fails_closed_without_leaking_provider_errors() -> (
    None
):
    service = runtime(httpx.AsyncClient())
    service.models.clear()
    result = request(service, payload())
    assert result.status_code == 503
    assert service.active == 0


def test_invalid_branch_does_not_route_or_try_another_model() -> None:
    service = runtime(httpx.AsyncClient())
    service.models["local"] = TestModel(
        custom_output_args={"branch": "invented"}
    )
    result = request(service, payload())
    assert result.status_code == 502
    assert "invented" not in result.text
    assert service.active == 0
    assert isinstance(service.models["api"], TestModel)
    assert service.models["api"].last_model_request_parameters is None


def test_fallback_removed_during_a_request_does_not_crash_or_leave_capacity_reserved() -> (
    None
):
    service = runtime(httpx.AsyncClient())

    def unavailable(messages: object, info: object) -> object:
        service.models.pop("api")
        raise ModelHTTPError(404, "removed-model")

    service.models["local"] = FunctionModel(unavailable)
    result = request(service, payload(fallback_models=["api"]))
    assert result.status_code == 503
    assert service.active == 0


def test_model_timeout_can_fall_back_within_the_total_deadline() -> None:
    service = runtime(httpx.AsyncClient())

    async def slow(messages: object, info: object) -> object:
        await asyncio.sleep(1)
        raise AssertionError(
            "The model call must be cancelled before completion"
        )

    service.models["local"] = FunctionModel(slow)
    result = request(
        service, payload(fallback_models=["api"], timeout_seconds=0.1)
    )
    assert result.status_code == 200
    assert result.json()["fallback"] is True
    assert service.active == 0


def test_explicit_fallback_policy_does_not_try_unlisted_models() -> None:
    service = runtime(httpx.AsyncClient())
    service.models.pop("local")
    result = request(service, payload(fallback_models=["not-installed"]))
    assert result.status_code == 503
    assert service.active == 0


def test_empty_fallback_list_disables_automatic_model_substitution() -> None:
    service = runtime(httpx.AsyncClient())
    service.models.pop("local")
    result = request(service, payload(fallback_models=[]))
    assert result.status_code == 503


def test_unavailable_model_adapter_can_fall_back_without_leaking_its_error() -> (
    None
):
    service = runtime(httpx.AsyncClient())

    def unavailable(messages: object, info: object) -> object:
        raise ModelAPIError("local", "private-config-details")

    service.models["local"] = FunctionModel(unavailable)
    result = request(service, payload())
    assert result.status_code == 200
    assert result.json()["model_alias"] == "api"
    assert "private-config-details" not in result.text


def test_fallback_attempts_are_bounded_even_with_a_larger_catalog() -> None:
    service = runtime(httpx.AsyncClient())
    calls: list[str] = []

    def unavailable(messages: object, info: object) -> object:
        calls.append("attempt")
        raise ModelHTTPError(404, "removed")

    for alias in ("local", "api", "another", "last"):
        service.settings.models[alias] = service.settings.models[
            "local"
        ].model_copy()
        service.models[alias] = FunctionModel(unavailable)
    result = request(service, payload())
    assert result.status_code == 503
    assert len(calls) == 3
    assert service.active == 0


def test_routing_uses_the_shared_scribe_concurrency_budget() -> None:
    service = runtime(httpx.AsyncClient())
    service.active = service.settings.max_concurrent_runs
    result = request(service, payload())
    assert result.status_code == 429
    assert service.active == service.settings.max_concurrent_runs


@pytest.mark.parametrize("status", [401, 403])
def test_provider_authorization_failure_is_terminal(status: int) -> None:
    service = runtime(httpx.AsyncClient())

    def denied(messages: object, info: object) -> object:
        raise ModelHTTPError(status, "local", body="secret")

    service.models["local"] = FunctionModel(denied)
    result = request(service, payload(fallback_models=["api"]))
    assert result.status_code == 503
    assert "secret" not in result.text
    assert service.active == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
