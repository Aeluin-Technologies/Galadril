"""Persist scoped child tasks without sharing conversations or domain semantics."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Literal

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits

from galadril_scribe.dependencies import Dependencies

TaskId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class TaskSpec(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    id: TaskId
    skill_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    prompt: str = Field(default="", max_length=65536)
    evidence: tuple[int, ...] = Field(default=(), max_length=8)
    input_ids: tuple[TaskId, ...] = Field(default=(), max_length=512)
    depends_on: tuple[TaskId, ...] = Field(default=(), max_length=512)
    sealed: bool = False
    output_kind: Literal["internal", "draft"] = "internal"
    request_budget: int = Field(default=16, ge=1, le=256)
    role: TaskId = "research"

    @model_validator(mode="after")
    def scoped(self) -> TaskSpec:
        """Keep scheduling dependencies separate from permission to read artifacts."""
        if self.sealed and (
            self.prompt or not self.input_ids or self.output_kind != "internal"
        ):
            raise ValueError("Sealed tasks accept only released draft inputs")
        for values in (
            self.skill_ids,
            self.evidence,
            self.input_ids,
            self.depends_on,
        ):
            if len(set(values)) != len(values):
                raise ValueError("Task scopes must be unique")
        if self.id in (*self.input_ids, *self.depends_on) or any(
            index < 0 for index in self.evidence
        ):
            raise ValueError("Invalid task dependency or evidence index")
        return self


class TaskResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    id: TaskId
    content: str = Field(max_length=65536)
    output_kind: Literal["internal", "draft"] = "internal"
    report_id: str = Field(default="", max_length=64)


class Consumption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal(0), ge=0)

    def add(self, usage: RunUsage) -> None:
        """Accumulate billable consumption, including unsuccessful attempts."""
        self.requests += usage.requests
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.tool_calls += usage.tool_calls
        self.cost += usage.cost or Decimal(0)


class TaskCheckpoint(Consumption):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    fingerprint: str
    status: Literal["running", "interrupted", "complete"]
    result: TaskResult | None = None
    spec: TaskSpec | None = None


ExecuteTask = Callable[
    [TaskSpec, tuple[TaskResult, ...], UsageLimits, RunUsage], Awaitable[str]
]
StateAccess = Callable[[str, JsonValue | None], Awaitable[JsonValue]]


class WorkflowToolset(FunctionToolset[Dependencies]):
    """Trusted plugin code supplies policy; model arguments cannot widen it."""

    def __init__(
        self,
        *,
        instructions: str,
        skill_ids: tuple[str, ...],
        research_factory: Callable[[], AbstractToolset[Dependencies]],
        replayable_names: tuple[str, ...] = (),
        role_instructions: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.instructions = instructions
        self.skill_ids = skill_ids
        self.research_factory = research_factory
        self.namespace = ""
        self.replayable_names = replayable_names
        self.role_instructions = role_instructions or {}

    def replayable_tools(self) -> tuple[str, ...]:
        """Expose only replay guarantees declared by installed plugin code."""
        return tuple(
            self.namespace + "_" + name
            for name in self.replayable_names
            if name in self.tools
        )


class TaskGraph:
    """Reserve model capacity before concurrency and persist each immutable scope."""

    __slots__ = (
        "execute",
        "storage",
        "usage",
        "limits",
        "semaphore",
        "reserve",
        "max_nodes",
        "results",
        "specs",
        "locks",
        "held_requests",
        "held_tokens",
        "held_cost",
        "prior",
        "recovered",
        "spent",
        "charged",
    )

    def __init__(
        self,
        execute: ExecuteTask,
        storage: StateAccess,
        usage: RunUsage,
        limits: UsageLimits,
        *,
        concurrency: int = 4,
        coordinator_reserve: int = 2,
        max_nodes: int = 4096,
        prior_consumption: Consumption | None = None,
    ) -> None:
        if concurrency < 1 or max_nodes < 1:
            raise ValueError("Task limits must be positive")
        self.execute, self.storage, self.usage, self.limits = (
            execute,
            storage,
            usage,
            limits,
        )
        self.semaphore = asyncio.Semaphore(concurrency)
        self.reserve, self.max_nodes = coordinator_reserve, max_nodes
        self.results: dict[str, TaskResult] = {}
        self.specs: dict[str, TaskSpec] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.held_requests = self.held_tokens = 0
        self.held_cost = Decimal(0)
        self.prior = prior_consumption or Consumption()
        self.recovered, self.spent = Consumption(), Consumption()
        self.charged: set[str] = set()

    def consumption(self) -> Consumption:
        """Reconcile child checkpoints with the last coordinator usage snapshot."""
        return Consumption(
            requests=max(self.prior.requests, self.recovered.requests)
            + self.spent.requests,
            input_tokens=max(
                self.prior.input_tokens, self.recovered.input_tokens
            )
            + self.spent.input_tokens,
            output_tokens=max(
                self.prior.output_tokens, self.recovered.output_tokens
            )
            + self.spent.output_tokens,
            tool_calls=max(self.prior.tool_calls, self.recovered.tool_calls)
            + self.spent.tool_calls,
            cost=max(self.prior.cost, self.recovered.cost) + self.spent.cost,
        )

    async def run_many(
        self, specs: tuple[TaskSpec, ...]
    ) -> tuple[TaskResult, ...]:
        """Cancel and join sibling tasks before propagating an interrupted batch."""
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self.run(spec)) for spec in specs]
        return tuple(task.result() for task in tasks)

    def _recover_consumption(
        self, identifier: str, checkpoint: TaskCheckpoint
    ) -> None:
        """Charge completed child work once, beyond the coordinator's saved ledger."""
        if identifier in self.charged:
            return
        before = self.consumption()
        self.charged.add(identifier)
        self.recovered.add(
            RunUsage(
                requests=checkpoint.requests,
                input_tokens=checkpoint.input_tokens,
                output_tokens=checkpoint.output_tokens,
                tool_calls=checkpoint.tool_calls,
                cost=checkpoint.cost,
            )
        )
        after = self.consumption()
        self.usage.incr(
            RunUsage(
                requests=after.requests - before.requests,
                input_tokens=after.input_tokens - before.input_tokens,
                output_tokens=after.output_tokens - before.output_tokens,
                tool_calls=after.tool_calls - before.tool_calls,
                cost=after.cost - before.cost,
            )
        )

    def _task_lock(self, identifier: str) -> asyncio.Lock:
        """Bound the durable graph independently of the concurrency ceiling."""
        if identifier not in self.locks:
            if len(self.locks) >= self.max_nodes:
                raise ValueError("Execution graph node budget exhausted")
            self.locks[identifier] = asyncio.Lock()
        return self.locks[identifier]

    def _inputs(self, spec: TaskSpec) -> tuple[TaskResult, ...]:
        """Check ordering without implicitly granting access to dependency outputs."""
        dependencies = (*spec.input_ids, *spec.depends_on)
        if any(key not in self.results for key in dependencies):
            raise ValueError("Task dependencies must complete before execution")
        inputs = tuple(self.results[key] for key in spec.input_ids)
        if spec.sealed and any(item.output_kind != "draft" for item in inputs):
            raise ValueError(
                "Independent reviewers receive only released draft artifacts"
            )
        return inputs

    async def _checkpoint(
        self, spec: TaskSpec, inputs: tuple[TaskResult, ...]
    ) -> TaskCheckpoint:
        """Refuse checkpoint reuse when either authority or an input has changed."""
        fingerprint = hashlib.sha256(
            (
                spec.model_dump_json()
                + "\0"
                + "\0".join(item.model_dump_json() for item in inputs)
            ).encode()
        ).hexdigest()
        saved = await self.storage("native:task:" + spec.id, None)
        if saved is None:
            return TaskCheckpoint(
                fingerprint=fingerprint, status="running", spec=spec
            )
        checkpoint = (
            TaskCheckpoint.model_validate_json(saved)
            if isinstance(saved, str)
            else TaskCheckpoint.model_validate(saved)
        )
        if checkpoint.fingerprint != fingerprint:
            raise ValueError("Task scope changed; create a new revision")
        self._recover_consumption(spec.id, checkpoint)
        return checkpoint

    def _reservation(self, spec: TaskSpec) -> UsageLimits:
        """Reserve model capacity before yielding so concurrent tasks cannot overspend.

        Tool calls remain unlimited; model requests, tokens and cost retain their
        independent ceilings and a small coordinator reserve.
        """
        available = (
            (self.limits.request_limit or 65536)
            - self.usage.requests
            - self.held_requests
            - self.reserve
        )
        if available < 1:
            raise ValueError(
                "Model request budget exhausted; checkpoint retained"
            )
        requests = min(spec.request_budget, available)
        fraction = Decimal(requests) / Decimal(max(1, available + self.reserve))
        tokens = self._token_allocation(fraction)
        cost = self._cost_allocation(fraction)
        allocation = UsageLimits(
            request_limit=requests,
            total_tokens_limit=tokens,
            cost_limit=cost,
            tool_calls_limit=None,
        )
        self.held_requests += requests
        self.held_tokens += tokens or 0
        self.held_cost += cost or Decimal(0)
        return allocation

    def _token_allocation(self, fraction: Decimal) -> int | None:
        """Partition the unreserved token capacity while keeping exhaustion explicit."""
        ceiling = self.limits.total_tokens_limit
        if ceiling is None:
            return None
        remaining = ceiling - self.usage.total_tokens - self.held_tokens
        tokens = int(Decimal(remaining) * fraction)
        if tokens < 1:
            raise ValueError(
                "Model consumption budget exhausted; checkpoint retained"
            )
        return tokens

    def _cost_allocation(self, fraction: Decimal) -> Decimal | None:
        """Partition the unreserved monetary capacity without floating point drift."""
        ceiling = self.limits.cost_limit
        if ceiling is None:
            return None
        cost = (
            ceiling - (self.usage.cost or Decimal(0)) - self.held_cost
        ) * fraction
        if cost <= 0:
            raise ValueError(
                "Model consumption budget exhausted; checkpoint retained"
            )
        return cost

    async def _execute(
        self,
        spec: TaskSpec,
        inputs: tuple[TaskResult, ...],
        checkpoint: TaskCheckpoint,
    ) -> TaskResult:
        """Persist the start and outcome even when a child is cancelled or fails."""
        allocation = self._reservation(spec)
        child_usage = RunUsage()
        checkpoint.status = "running"
        self.charged.add(spec.id)
        try:
            await self.storage(
                "native:task:" + spec.id, checkpoint.model_dump_json()
            )
            result = await self._invoke(spec, inputs, allocation, child_usage)
            checkpoint.result, checkpoint.status = result, "complete"
            return result
        finally:
            self.held_requests -= allocation.request_limit or 0
            self.held_tokens -= allocation.total_tokens_limit or 0
            self.held_cost -= allocation.cost_limit or Decimal(0)
            self.usage.incr(child_usage)
            self.spent.add(child_usage)
            checkpoint.add(child_usage)
            if checkpoint.status != "complete":
                checkpoint.status = "interrupted"
            await self.storage(
                "native:task:" + spec.id, checkpoint.model_dump_json()
            )

    async def _invoke(
        self,
        spec: TaskSpec,
        inputs: tuple[TaskResult, ...],
        allocation: UsageLimits,
        usage: RunUsage,
    ) -> TaskResult:
        """Trace task structure and consumption without recording private content."""
        with trace.get_tracer("galadril.scribe").start_as_current_span(
            "scribe.workflow.task",
            attributes={
                "task.id": spec.id,
                "task.sealed": spec.sealed,
                "task.dependencies": len(spec.input_ids) + len(spec.depends_on),
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            content = await self.execute(spec, inputs, allocation, usage)
            span.set_attribute("task.requests", usage.requests)
        return TaskResult(
            id=spec.id,
            content=content,
            output_kind=spec.output_kind,
            report_id=hashlib.sha256(
                (spec.id + "\0" + content).encode()
            ).hexdigest(),
        )

    async def run(self, spec: TaskSpec) -> TaskResult:
        """Execute or restore one immutable task after its prerequisites complete.

        Args:
            spec: Scope and budgets supplied by a trusted workflow adapter.

        Returns:
            The completed artifact, including its stable public report identity.

        Raises:
            ValueError: A scope, dependency or consumption limit is invalid.
        """
        async with self._task_lock(spec.id):
            previous = self.specs.get(spec.id)
            if previous is not None and previous != spec:
                raise ValueError("Task scope changed; create a new revision")
            self.specs[spec.id] = spec
            if spec.id in self.results:
                return self.results[spec.id]
            inputs = self._inputs(spec)
            checkpoint = await self._checkpoint(spec, inputs)
            if (
                checkpoint.status == "complete"
                and checkpoint.result is not None
            ):
                self.results[spec.id] = checkpoint.result
                return checkpoint.result
            async with self.semaphore:
                result = await self._execute(spec, inputs, checkpoint)
            self.results[spec.id] = result
            return result
