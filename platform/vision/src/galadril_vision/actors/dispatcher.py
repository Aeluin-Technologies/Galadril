"""Non-blocking FastStream-to-Ray dispatch with trace propagation."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from typing import Protocol, TypeVar, cast

from galadril_pipeline.events import PipelineCommand, ResourceClass, StepResult
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from galadril_vision.telemetry.context import inject_trace_context
from galadril_vision.telemetry.metrics import PipelineMetrics

T = TypeVar("T")


class RemoteMethod(Protocol):
    """Structural type for a Ray actor method handle."""

    def remote(
        self, command_json: bytes, carrier: Mapping[str, str]
    ) -> object: ...


class ActorHandle(Protocol):
    """Minimal Ray actor interface required by the dispatcher."""

    @property
    def execute(self) -> RemoteMethod: ...


class RayActorDispatcher:
    """Dispatches commands round-robin while never blocking the asyncio loop."""

    __slots__ = ("_actors", "_indices", "_metrics")

    def __init__(
        self,
        actors: Mapping[ResourceClass, Sequence[ActorHandle]],
        metrics: PipelineMetrics,
    ) -> None:
        """Validates that every configured execution pool has at least one actor."""
        actor_pools = {
            resource: tuple(pool) for resource, pool in actors.items()
        }
        empty = [
            resource.value for resource, pool in actor_pools.items() if not pool
        ]
        if empty:
            raise ValueError(
                f"Ray actor pools cannot be empty: {sorted(empty)}"
            )
        self._actors = actor_pools
        self._indices = {resource: 0 for resource in actor_pools}
        self._metrics = metrics

    async def dispatch(self, command: PipelineCommand) -> StepResult:
        """Awaits a Ray ObjectRef cooperatively and validates its result contract."""
        actor = self._next_actor(command.resource_class)
        labels = {
            "pipeline": command.pipeline,
            "step": command.step,
            "resource_class": command.resource_class.value,
        }
        tracer = trace.get_tracer("galadril.ray.dispatcher")
        with tracer.start_as_current_span(
            "ray.dispatch",
            kind=SpanKind.PRODUCER,
            attributes={
                "messaging.message.id": str(command.event_id),
                "galadril.pipeline": command.pipeline,
                "galadril.pipeline.step": command.step,
                "ray.resource_class": command.resource_class.value,
            },
        ):
            carrier = inject_trace_context()
            self._metrics.ray_task_started(**labels)
            try:
                reference = actor.execute.remote(
                    command.model_dump_json().encode("utf-8"), carrier
                )
                payload = await _await_reference(reference)
                result = StepResult.model_validate_json(payload)
            except Exception:
                self._metrics.ray_task_completed(**labels, outcome="failed")
                raise
            self._metrics.ray_task_completed(**labels, outcome="completed")
            return result

    def _next_actor(self, resource: ResourceClass) -> ActorHandle:
        """Returns an actor in constant time using a bounded integer cursor."""
        try:
            pool = self._actors[resource]
        except KeyError as error:
            raise RuntimeError(
                f"No Ray actor pool configured for '{resource.value}'"
            ) from error
        index = self._indices[resource]
        self._indices[resource] = (index + 1) % len(pool)
        return pool[index]


async def _await_reference(reference: object) -> bytes:
    """Awaits Ray ObjectRef and test-compatible awaitables without ray.get()."""
    if not hasattr(reference, "__await__"):
        raise TypeError(
            "Ray actor method did not return an awaitable ObjectRef"
        )
    return await cast(Awaitable[bytes], reference)
