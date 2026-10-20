"""Native harness capabilities share public progress and generation-scoped policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from itertools import chain

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, on_event
from pydantic_ai.messages import (
    CapabilityEvent,
    FunctionToolCallEvent,
    ToolReturn,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai_harness import (
    AskUser,
    GuardrailResult,
    InputGuardrail,
    OutputGuardrail,
    Planning,
    ToolGuardrail,
)
from pydantic_ai_harness.ask_user import (
    Answerer as Answerer,
)
from pydantic_ai_harness.ask_user import (
    AskUserAnsweredEvent,
)
from pydantic_ai_harness.guardrails import ToolCallInfo, ToolResultInfo
from pydantic_ai_harness.guardrails.detectors import (
    for_tool_result_text,
    redact_secrets,
)
from pydantic_ai_harness.planning import (
    PlanCompletedEvent,
    PlanCreatedEvent,
    PlanDeletedEvent,
    PlanStatusChangedEvent,
    PlanUpdatedEvent,
)

from galadril_scribe.activity import (
    PhaseActivity,
    PlanActivity,
    PlanTask,
    QuestionResolved,
)
from galadril_scribe.dependencies import Dependencies

ActivitySink = Callable[[str], Awaitable[None]]
PlanChange = (
    PlanCompletedEvent
    | PlanCreatedEvent
    | PlanDeletedEvent
    | PlanStatusChangedEvent
    | PlanUpdatedEvent
)


def _budget_instructions(ctx: RunContext[Dependencies]) -> str:
    """Expose remaining shared limits before planning consumes the research budget."""
    limits = ctx.usage_limits
    if limits is None:
        return (
            "Reuse collected evidence and do not retry an unavailable source."
        )
    tools = (
        max(0, limits.tool_calls_limit - ctx.usage.tool_calls)
        if limits.tool_calls_limit is not None
        else None
    )
    requests = (
        max(0, limits.request_limit - ctx.usage.requests)
        if limits.request_limit is not None
        else None
    )
    warning = (
        "Conclude now using verified evidence already available, or explain what "
        "is missing. Do not start another research or planning cycle."
        if any(value is not None and value <= 3 for value in (tools, requests))
        else "Keep plans concise, reuse results and do not repeat unchanged searches."
    )
    return (
        f"Remaining tool calls: {tools}; remaining model requests: {requests}. "
        "Planning and questions also consume tool calls. " + warning
    )


class PolicyFailure(ValueError):
    """A value cannot cross the model or presentation boundary safely."""


@dataclass(frozen=True, slots=True, repr=False)
class SecretPolicy:
    secrets: tuple[str, ...]
    _dynamic: set[str] = field(default_factory=set, compare=False, repr=False)

    def register_secret(self, value: str) -> None:
        """Rotating OAuth tokens must join the same run's output and tool guardrails."""
        if (
            not value
            or len(value) > 32768
            or (value not in self._dynamic and len(self._dynamic) >= 16)
        ):
            raise PolicyFailure("Provider credential guard exceeds limit")
        self._dynamic.add(value)

    def public_text(self, text: str) -> str:
        for secret in chain(self.secrets, self._dynamic):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        result = redact_secrets(text)
        if result.action == "replace":
            if not isinstance(result.replacement, str):
                raise PolicyFailure("Unsupported text replacement")
            return result.replacement
        if result.action != "allow":
            raise PolicyFailure("Text rejected")
        return text

    def sensitive(self, value: object, depth: int = 0) -> bool:
        if depth > 16:
            raise PolicyFailure("Policy nesting exceeds limit")
        if isinstance(value, str):
            return self.public_text(value) != value
        if isinstance(value, ToolReturn):
            return self.sensitive(
                value.return_value, depth + 1
            ) or self.sensitive(value.content, depth + 1)
        if isinstance(value, BaseModel):
            return self.sensitive(value.model_dump(), depth + 1)
        if isinstance(value, Mapping):
            if len(value) > 4096:
                raise PolicyFailure("Policy value exceeds limit")
            return any(
                self.sensitive(item, depth + 1) for item in value.values()
            )
        if isinstance(value, (tuple, list)):
            if len(value) > 4096:
                raise PolicyFailure("Policy value exceeds limit")
            return any(self.sensitive(item, depth + 1) for item in value)
        return False

    def guard(self, value: object) -> GuardrailResult:
        if isinstance(value, str):
            sanitized = self.public_text(value)
            return (
                GuardrailResult.allow()
                if sanitized == value
                else GuardrailResult.replace(sanitized)
            )
        return (
            GuardrailResult.block("Sensitive data rejected")
            if self.sensitive(value)
            else GuardrailResult.allow()
        )

    def arguments(self, call: ToolCallInfo) -> GuardrailResult:
        items = call.args.get("items")
        if (
            call.name == "write_plan"
            and isinstance(items, list)
            and len(items) > 64
        ):
            return GuardrailResult.block("Plan exceeds 64 tasks")
        return (
            GuardrailResult.block("Sensitive tool arguments rejected")
            if self.sensitive(call.args)
            else GuardrailResult.allow()
        )

    def result(self, call: ToolResultInfo) -> GuardrailResult:
        if (
            isinstance(call.result, str)
            or isinstance(call.result, ToolReturn)
            and isinstance(call.result.return_value, str)
        ):
            return for_tool_result_text(self.guard)(call)
        return self.guard(call.result)

    async def publish(self, emit: ActivitySink, content: str) -> None:
        if len(content.encode()) > 65536 or self.sensitive(content):
            raise PolicyFailure("Activity rejected")
        await emit(content)


@dataclass
class Progress(AbstractCapability["Dependencies"]):
    emit: ActivitySink
    policy: SecretPolicy
    agent_id: str | None = None

    def get_instructions(self) -> Callable[[RunContext[Dependencies]], str]:
        return _budget_instructions

    async def before_model_request(
        self,
        ctx: RunContext[Dependencies],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        await self.emit(
            PhaseActivity(
                phase="thinking", agent_id=self.agent_id
            ).model_dump_json()
        )
        return request_context

    @on_event(
        PlanCreatedEvent,
        PlanUpdatedEvent,
        PlanStatusChangedEvent,
        PlanCompletedEvent,
        PlanDeletedEvent,
    )
    async def plan(
        self, ctx: RunContext[Dependencies], event: CapabilityEvent
    ) -> None:
        if not isinstance(
            event,
            (
                PlanCompletedEvent,
                PlanCreatedEvent,
                PlanDeletedEvent,
                PlanStatusChangedEvent,
                PlanUpdatedEvent,
            ),
        ):
            return
        item = event.item
        activity = PlanActivity(
            operation="delete"
            if isinstance(event, PlanDeletedEvent)
            else "upsert",
            agent_id=self.agent_id,
            task=PlanTask(
                id=item.id,
                content=item.content,
                status=item.status.value,
                active_form=item.active_form,
                parent_id=item.parent_id,
            ),
        )
        await self.policy.publish(self.emit, activity.model_dump_json())

    @on_event(FunctionToolCallEvent)
    async def tool(
        self, ctx: RunContext[Dependencies], event: FunctionToolCallEvent
    ) -> None:
        await self.emit(
            PhaseActivity(
                phase="tool", agent_id=self.agent_id
            ).model_dump_json()
        )

    @on_event(AskUserAnsweredEvent)
    async def question_timeout(
        self, ctx: RunContext[Dependencies], event: AskUserAnsweredEvent
    ) -> None:
        if event.timed_out:
            await self.emit(
                QuestionResolved(
                    id=event.request_id, cancelled=True
                ).model_dump_json()
            )


def capabilities(
    policy: SecretPolicy,
    emit: ActivitySink,
    *,
    answerer: Answerer | None = None,
    agent_id: str | None = None,
) -> list[AbstractCapability[Dependencies]]:
    result: list[AbstractCapability[Dependencies]] = [
        Planning[Dependencies](
            tools=("write_plan", "read_plan", "update_task_status")
        ),
        Progress(emit=emit, policy=policy, agent_id=agent_id),
        InputGuardrail[Dependencies](guard=policy.guard),
        ToolGuardrail[Dependencies](
            guard=policy.arguments, result_guard=policy.result
        ),
        OutputGuardrail[Dependencies](guard=policy.guard),
    ]
    if answerer is not None:
        result.append(AskUser[Dependencies](answerer=answerer, timeout=120))
    return result
