"""Native agent adapters preserve PydanticAI features behind bounded execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserContent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage, UsageLimits

if TYPE_CHECKING:
    from galadril_scribe.dependencies import Dependencies


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    prompt: Sequence[UserContent]
    mission: str
    deps: Dependencies
    model: Model
    model_settings: ModelSettings
    toolsets: Sequence[AbstractToolset[Dependencies]]
    capabilities: Sequence[AbstractCapability[Dependencies]]
    usage: RunUsage
    limits: UsageLimits


class AgentBinding(Protocol):
    async def execute(self, invocation: AgentInvocation) -> str: ...


def _report_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    raise ValueError("Native output requires an explicit report serializer")


@dataclass(frozen=True, slots=True)
class NativeAgentBinding[Output]:
    """Keeps output schemas, validators, hooks and retries in the native agent."""

    agent: AbstractAgent[Dependencies, Output]
    serialize: Callable[[Output], str] = _report_text

    async def execute(self, invocation: AgentInvocation) -> str:
        result = await self.agent.run(
            invocation.prompt,
            instructions=invocation.mission,
            deps=invocation.deps,
            model=invocation.model,
            model_settings=invocation.model_settings,
            toolsets=invocation.toolsets,
            capabilities=invocation.capabilities,
            usage_limits=invocation.limits,
            usage=invocation.usage,
        )
        return self.serialize(result.output)


async def execute_agent(
    binding: AgentBinding,
    invocation: AgentInvocation,
    *,
    team_id: str,
    agent_id: str,
) -> str:
    """Measures execution without recording inputs, outputs or exception payloads."""
    usage = invocation.usage
    requests_before = usage.requests
    tools_before = usage.tool_calls
    with trace.get_tracer("galadril.scribe").start_as_current_span(
        "scribe.team.agent",
        attributes={"scribe.team.id": team_id, "scribe.agent.id": agent_id},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            report = await binding.execute(invocation)
        except (Exception, asyncio.CancelledError) as error:
            span.set_status(StatusCode.ERROR)
            span.set_attribute("error.type", type(error).__name__)
            raise
        else:
            span.set_status(StatusCode.OK)
            return report
        finally:
            span.set_attribute(
                "scribe.usage.requests", usage.requests - requests_before
            )
            span.set_attribute(
                "scribe.usage.tool_calls", usage.tool_calls - tools_before
            )
