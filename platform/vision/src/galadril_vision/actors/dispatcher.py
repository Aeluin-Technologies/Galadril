"""Non-blocking FastStream-to-Ray dispatch with trace propagation."""

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Awaitable, Mapping, Sequence
from time import perf_counter
from typing import Protocol, TypeVar, cast

from galadril_pipeline.events import (
    EventStatus,
    Failure,
    PipelineCommand,
    ResourceClass,
    StepResult,
)
from galadril_pipeline.telemetry_contract import (
    SPAN_VISION_STEP_DISPATCH,
    ErrorCode,
    VisionOutcome,
    VisionPhase,
    VisionTimingPhase,
    vision_step_dispatch_attributes,
)
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from ray.exceptions import ActorDiedError, ActorUnavailableError, RayTaskError

from galadril_vision.application.errors import (
    CommandProcessingError,
    execution_failure,
)
from galadril_vision.telemetry.context import inject_trace_context
from galadril_vision.telemetry.failures import record_failure
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
        tracer = trace.get_tracer("galadril.ray.dispatcher")
        with tracer.start_as_current_span(
            SPAN_VISION_STEP_DISPATCH,
            kind=SpanKind.PRODUCER,
            attributes=vision_step_dispatch_attributes(
                messaging_message_id=str(command.event_id),
                pipeline_attempt=command.attempt,
                vision_resource_class=command.resource_class,
            ),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            carrier = inject_trace_context()
            self._metrics.ray_task_started(command.resource_class)
            outcome = VisionOutcome.FAILED
            phase = VisionPhase.DISPATCH
            try:
                started = perf_counter()
                command_json = command.model_dump_json().encode("utf-8")
                submitted = perf_counter()
                self._metrics.phase_completed(
                    phase=VisionTimingPhase.SERIALIZATION,
                    resource_class=command.resource_class,
                    duration_seconds=submitted - started,
                )
                submitted = perf_counter()
                try:
                    reference = actor.execute.remote(command_json, carrier)
                    payload = await _await_reference(reference)
                finally:
                    self._metrics.phase_completed(
                        phase=VisionTimingPhase.RAY_ROUND_TRIP,
                        resource_class=command.resource_class,
                        duration_seconds=perf_counter() - submitted,
                    )
                phase = VisionPhase.RESULT
                result = StepResult.model_validate_json(payload)
                if result.failure is not None:
                    record_failure(
                        command, result.failure, VisionPhase.DISPATCH, span=span
                    )
                elif result.status is EventStatus.COMPLETED:
                    outcome = VisionOutcome.COMPLETED
                return result
            except CancelledError:
                outcome = VisionOutcome.CANCELLED
                raise
            except Exception as error:
                if isinstance(error, RayTaskError) and isinstance(
                    error.cause, CommandProcessingError
                ):
                    failure = error.cause.failure
                elif isinstance(error, (ActorDiedError, ActorUnavailableError)):
                    failure = Failure(code=ErrorCode.UNAVAILABLE, phase=phase)
                else:
                    failure = execution_failure(error, phase)
                record_failure(command, failure, phase, span=span)
                raise CommandProcessingError(
                    failure.code.value, failure=failure
                ) from None
            finally:
                self._metrics.ray_task_completed(
                    command.resource_class, outcome
                )

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
