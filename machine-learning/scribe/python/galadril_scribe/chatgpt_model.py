"""Fail over one model request, retaining the agent's completed tool trajectory."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

import httpx
from openai import AsyncOpenAI
from opentelemetry import metrics
from pydantic import SecretStr
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import (
    CompletedStreamedResponse,
    Model,
    ModelRequestParameters,
    StreamedResponse,
)
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles import merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from galadril_scribe.chatgpt_accounts import AccessCredential

_FORBIDDEN = frozenset(
    {
        "background",
        "conversation",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "moderation",
        "multi_agent",
        "prompt",
        "prompt_cache_retention",
        "safety_identifier",
        "temperature",
        "top_logprobs",
        "top_p",
        "truncation",
        "user",
        "previous_response_id",
    }
)
_QUOTA_CODE = "subscription_sharing_usage_limit_exceeded"


class AccountAccess(Protocol):
    async def access(self, owner: str) -> AccessCredential | None: ...
    async def models(self, owner: str) -> tuple[str, ...]: ...
    async def exhausted(self, owner: str, registration: str) -> None: ...


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for part in self.response.aiter_bytes():
            total += len(part)
            if total > 2 * 1024 * 1024:
                raise ModelAPIError("openai", "Provider stream exceeds limit")
            yield part

    async def aclose(self) -> None:
        await self.response.aclose()


class _PlanTransport(httpx.AsyncBaseTransport):
    """Adapt the preview wire contract without changing the shared API transport."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        if str(request.url) != "https://api.openai.com/v1/responses":
            raise ModelAPIError("openai", "Unsupported plan endpoint")
        if len(request.content) > 1024 * 1024:
            raise ModelAPIError("openai", "Model request exceeds limit")
        payload = json.loads(request.content)
        if not isinstance(payload, dict) or payload.get("stream") is not True:
            raise ModelAPIError("openai", "Plan requests require streaming")
        for field in _FORBIDDEN:
            payload.pop(field, None)
        payload["store"] = False
        inputs = payload.get("input")
        if not isinstance(inputs, list):
            raise ModelAPIError(
                "openai", "Plan requests require explicit history"
            )
        for item in inputs:
            if isinstance(item, dict) and item.get("role") == "system":
                item["role"] = "developer"
        declarations: list[dict[str, object]] = []
        native: list[dict[str, object]] = []
        for tool in payload.pop("tools", []):
            if tool.get("type") in {"function", "custom"}:
                tool.pop("defer_loading", None)
                declarations.append(tool)
            elif tool.get("type") == "web_search":
                native.append(tool)
            else:
                raise ModelAPIError("openai", "Unsupported hosted plan tool")
        # Additional-tools items preserve default-namespace calls across API failover.
        if declarations:
            inputs.insert(
                0,
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": declarations,
                },
            )
        if native:
            payload["tools"] = native
        headers = dict(request.headers)
        headers.pop("content-length", None)
        forwarded = httpx.Request(
            "POST",
            str(request.url),
            headers=headers,
            content=json.dumps(payload, separators=(",", ":")).encode(),
        )
        response = await self.client.send(
            forwarded, stream=True, follow_redirects=False
        )
        response_headers = dict(response.headers)
        response_headers.pop("content-encoding", None)
        response_headers.pop("content-length", None)
        return httpx.Response(
            response.status_code,
            headers=response_headers,
            stream=_BoundedStream(response),
            request=request,
        )


def _quota(error: ModelAPIError) -> bool:
    if isinstance(error, ModelHTTPError):
        body = error.body
        if error.status_code != 429 or not isinstance(body, dict):
            return False
        details = body.get("error", body)
        return isinstance(details, dict) and details.get("code") == _QUOTA_CODE
    return error.message.startswith(_QUOTA_CODE + ":")


class ChatGPTModel(Model):
    """Buffer a provider turn before exposing it, so failed partial tool calls vanish."""

    def __init__(
        self,
        model: str,
        *,
        accounts: AccountAccess,
        owner: str,
        api_key: SecretStr | None,
        client: httpx.AsyncClient,
        emit: Callable[[str], Awaitable[None]],
        profile: OpenAIModelProfile,
        protect: Callable[[str], None] | None = None,
    ) -> None:
        compatible = merge_profile(
            profile,
            OpenAIModelProfile(
                openai_system_prompt_role="developer",
                openai_responses_requires_store_false=True,
                supported_native_tools=frozenset(),
            ),
        )
        super().__init__(profile=compatible)
        self._model_name = model
        self.accounts = accounts
        self.owner = owner
        self.api_key = api_key
        self.client = client
        self.emit = emit
        self.api_only = False
        self.protect = protect
        self.attempts = metrics.get_meter("galadril.scribe").create_counter(
            "scribe.chatgpt.model_requests"
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def system(self) -> str:
        return "openai"

    async def _billing(self, provider: str, reason: str) -> None:
        await self.emit(
            json.dumps(
                {"type": "billing", "provider": provider, "reason": reason},
                separators=(",", ":"),
            )
        )

    async def _complete(
        self,
        messages: list[ModelMessage],
        settings: ModelSettings | None,
        parameters: ModelRequestParameters,
    ) -> ModelResponse:
        credential = (
            None if self.api_only else await self.accounts.access(self.owner)
        )
        unavailable = False
        if (
            credential is not None
            and self.model_name not in await self.accounts.models(self.owner)
        ):
            credential = None
            unavailable = True
        if credential is not None:
            if self.protect is not None:
                self.protect(credential.token.get_secret_value())
            await self._billing("chatgpt", "connected")
            try:
                async with httpx.AsyncClient(
                    transport=_PlanTransport(self.client), trust_env=False
                ) as transport:
                    provider = OpenAIProvider(
                        openai_client=AsyncOpenAI(
                            api_key=credential.token.get_secret_value(),
                            base_url="https://api.openai.com/v1",
                            max_retries=0,
                            http_client=transport,
                        )
                    )
                    primary = OpenAIResponsesModel(
                        self.model_name, provider=provider, profile=self.profile
                    )
                    self.attempts.add(1, {"provider": "chatgpt"})
                    async with primary.request_stream(
                        messages, settings, parameters
                    ) as stream:
                        async for _event in stream:
                            pass
                        result = stream.get()
                        if (
                            result.state != "complete"
                            or result.finish_reason
                            in {"error", "length", "content_filter"}
                        ):
                            raise ModelAPIError(
                                self.model_name,
                                "Plan response did not complete",
                            )
                        return result
            except ModelAPIError as error:
                if not _quota(error):
                    raise
                await self.accounts.exhausted(
                    self.owner, credential.registration
                )
                self.api_only = True
                await self._billing(
                    "api" if self.api_key is not None else "chatgpt", "quota"
                )
        else:
            await self._billing(
                "api",
                "quota"
                if self.api_only
                else "model_unavailable"
                if unavailable
                else "not_connected",
            )
        if self.api_key is None:
            raise ModelAPIError(
                self.model_name, "No API fallback credentials are configured"
            )
        provider = OpenAIProvider(
            openai_client=AsyncOpenAI(
                api_key=self.api_key.get_secret_value(),
                base_url="https://api.openai.com/v1",
                max_retries=0,
                http_client=self.client,
            )
        )
        fallback = OpenAIResponsesModel(
            self.model_name,
            provider=provider,
            profile=self.profile,
            settings={"openai_store": False},
        )
        self.attempts.add(1, {"provider": "api"})
        async with fallback.request_stream(
            messages, settings, parameters
        ) as stream:
            async for _event in stream:
                pass
            result = stream.get()
            if result.state != "complete" or result.finish_reason in {
                "error",
                "length",
                "content_filter",
            }:
                raise ModelAPIError(
                    self.model_name, "API response did not complete"
                )
            return result

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        return await self._complete(
            messages, model_settings, model_request_parameters
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: object = None,
    ) -> AsyncIterator[StreamedResponse]:
        result = await self._complete(
            messages, model_settings, model_request_parameters
        )
        yield CompletedStreamedResponse(
            result,
            model_request_parameters=model_request_parameters,
            replay_events=True,
        )


class UnavailableModel(Model):
    """OAuth-only installations must never send a fabricated API credential."""

    def __init__(self, model: str) -> None:
        super().__init__()
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def system(self) -> str:
        return "openai"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        raise ModelAPIError(
            self.model_name,
            "Connect a ChatGPT account or configure an API credential",
        )
