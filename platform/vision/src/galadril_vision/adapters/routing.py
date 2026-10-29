"""Bounded regex and structured SLM decisions over declared pipeline branches."""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

import httpx
import regex
import structlog
from galadril_pipeline.branching import (
    ModelAlias,
    ReasoningEffort,
    RoutingDecision,
    RoutingRequest,
)
from galadril_pipeline.config import PipelineStep, RoutingStrategy
from galadril_pipeline.events import PipelineCommand
from opentelemetry import propagate, trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from galadril_vision.application.errors import CommandProcessingError


class RegexParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: str = Field(
        default="record.raw_payload.content", min_length=1, max_length=256
    )
    pattern: str = Field(min_length=1, max_length=4096)
    match_branch: str = Field(min_length=1, max_length=128)
    default_branch: str = Field(min_length=1, max_length=128)
    timeout_seconds: float = Field(default=0.02, gt=0, le=0.1)
    max_characters: int = Field(default=65536, ge=1, le=262144)


def _value(payload: dict[str, JsonValue], field: str) -> JsonValue:
    value: JsonValue = payload
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError("Routing field is missing")
        value = value[part]
    return value


def _text(payload: dict[str, JsonValue], field: str, limit: int) -> str:
    value = _value(payload, field)
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Routing field must be bounded text")
    return value


class RegexRouter:
    __slots__ = ("_params", "_pattern")

    def __init__(self, step: PipelineStep) -> None:
        self._params = RegexParams.model_validate(step.params.model_extra or {})
        if (
            not {self._params.match_branch, self._params.default_branch}
            <= step.branches.keys()
        ):
            raise ValueError("Regex routes must use declared branch labels")
        self._pattern = regex.compile(self._params.pattern)
        if self._pattern.groups > 32:
            raise ValueError("Regex extraction exceeds group limit")

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        del step
        params = self._params
        text = _text(command.payload, params.field, params.max_characters)
        match = await asyncio.to_thread(
            self._pattern.search, text, timeout=params.timeout_seconds
        )
        return {
            **command.payload,
            "branch": params.match_branch if match else params.default_branch,
            "extractions": dict(match.groupdict()) if match else {},
        }


class SlmParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: str = Field(
        default="record.raw_payload.content", min_length=1, max_length=256
    )
    metadata_fields: tuple[str, ...] = Field(default=(), max_length=16)
    instructions: str = Field(min_length=1, max_length=8192)
    fallback_models: tuple[ModelAlias, ...] | None = Field(
        default=None, max_length=8
    )
    reasoning_effort: ReasoningEffort | None = None
    timeout_seconds: float = Field(default=60, gt=0, le=180)
    max_characters: int = Field(default=16384, ge=1, le=65536)


class SlmRouter:
    """Reuses Scribe's provider pools instead of importing provider credentials."""

    __slots__ = ("_params", "_request", "_endpoint", "_client")

    def __init__(
        self,
        step: PipelineStep,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._params = SlmParams.model_validate(step.params.model_extra or {})
        self._request = RoutingRequest(
            text="",
            labels=tuple(step.branches),
            instructions=self._params.instructions,
            model_alias=step.model,
            fallback_models=self._params.fallback_models,
            reasoning_effort=self._params.reasoning_effort,
            timeout_seconds=self._params.timeout_seconds,
        )
        self._endpoint = endpoint + "/pipeline/route"
        self._client = client

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        del step
        text = _text(
            command.payload, self._params.field, self._params.max_characters
        )
        if self._params.metadata_fields:
            metadata: dict[str, JsonValue] = {}
            for field in self._params.metadata_fields:
                try:
                    metadata[field] = _value(command.payload, field)
                except ValueError:
                    metadata[field] = None
            text = json.dumps(
                {"content": text, "metadata": metadata}, ensure_ascii=False
            )
            if len(text) > 65536:
                raise ValueError("Routing content and metadata exceed limit")
        request = self._request.model_copy(update={"text": text})
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False, trust_env=False
            )
        headers: dict[str, str] = {"content-type": "application/json"}
        propagate.inject(headers)
        async with asyncio.timeout(self._params.timeout_seconds + 2):
            async with self._client.stream(
                "POST",
                self._endpoint,
                content=request.model_dump_json(),
                headers=headers,
                timeout=self._params.timeout_seconds + 2,
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for part in response.aiter_bytes():
                    if len(data) + len(part) > 4096:
                        raise ValueError("Routing decision exceeds limit")
                    data.extend(part)
        decision = RoutingDecision.model_validate_json(data)
        if decision.branch not in self._request.labels:
            raise ValueError("Scribe returned an undeclared branch")
        return {
            **command.payload,
            "branch": decision.branch,
            "routing_model": decision.model_alias,
            "routing_fallback": decision.fallback,
        }


class _DecisionHandler(Protocol):
    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]: ...


class MetadataParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conditions: dict[str, str | int | float | bool | None] = Field(
        min_length=1, max_length=16
    )
    match_branch: str = Field(min_length=1, max_length=128)
    default_branch: str = Field(min_length=1, max_length=128)


class MetadataRouter:
    """Matches typed scalar values; missing metadata is a non-match, not null."""

    __slots__ = ("_params",)

    def __init__(self, step: PipelineStep) -> None:
        self._params = MetadataParams.model_validate(
            step.params.model_extra or {}
        )
        if (
            not {self._params.match_branch, self._params.default_branch}
            <= step.branches.keys()
        ):
            raise ValueError("Metadata routes must use declared branch labels")

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        del step
        matches = True
        for field, expected in self._params.conditions.items():
            try:
                actual = _value(command.payload, field)
            except ValueError:
                matches = False
                break
            if type(actual) is not type(expected) or actual != expected:
                matches = False
                break
        return {
            **command.payload,
            "branch": self._params.match_branch
            if matches
            else self._params.default_branch,
        }


class ConditionRouter:
    """Compiles typed business rules once per immutable pipeline revision."""

    __slots__ = ("_params", "_filter")

    def __init__(self, step: PipelineStep) -> None:
        from galadril_pipeline.config import StepType
        from galadril_pipeline.config.conditions import ConditionParams

        self._params = ConditionParams.model_validate(
            step.params.model_extra or {}
        )
        self._filter = step.type is StepType.FILTER
        if (
            not self._filter
            and not {self._params.match_branch, self._params.default_branch}
            <= step.branches.keys()
        ):
            raise ValueError("Conditions require declared route branches")

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        """Carries the decision into durable completion so replay preserves it."""
        del step
        matches = self._params.matches(command.payload)
        if self._filter:
            return {**command.payload, "filter_accepted": matches}
        return {
            **command.payload,
            "branch": self._params.match_branch
            if matches
            else self._params.default_branch,
        }


class ForwardingStepAdapter:
    """Hands the same payload to an expanded child without creating an ingest job."""

    __slots__ = ()

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        del step
        return command.payload


class RoutingStepAdapter:
    """Compiles once per published step; transport still enforces tenant admission."""

    __slots__ = ("_handler",)

    def __init__(self, step: PipelineStep, endpoint: str) -> None:
        self._handler: _DecisionHandler
        match step.routing:
            case RoutingStrategy.REGEX:
                self._handler = RegexRouter(step)
            case RoutingStrategy.SLM:
                self._handler = SlmRouter(step, endpoint)
            case RoutingStrategy.METADATA:
                self._handler = MetadataRouter(step)
            case RoutingStrategy.CONDITIONS:
                self._handler = ConditionRouter(step)
            case _:
                raise ValueError("Unknown native routing strategy")

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        with trace.get_tracer("galadril.vision").start_as_current_span(
            "pipeline.route.decide",
            attributes={
                "pipeline.step": step.step,
                "routing.strategy": step.routing.value if step.routing else "",
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                return await self._handler.execute(command, step)
            except Exception as error:
                span.set_status(StatusCode.ERROR)
                structlog.get_logger(__name__).warning(
                    "pipeline.routing.failed",
                    step=step.step,
                    error_type=type(error).__name__,
                )
                raise CommandProcessingError(
                    "Native branch routing failed"
                ) from None
