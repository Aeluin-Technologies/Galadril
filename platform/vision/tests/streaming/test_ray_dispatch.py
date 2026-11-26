"""Unit tests for non-blocking Ray dispatch and actor trace boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from uuid import uuid4

import pytest
from galadril_pipeline.config import StepType
from galadril_pipeline.events import (
    EventStatus,
    PipelineCommand,
    ResourceClass,
    StepResult,
)
from galadril_pipeline.telemetry_contract import ErrorCode, VisionPhase
from galadril_vision.actors.dispatcher import RayActorDispatcher
from galadril_vision.actors.worker import PipelineActor
from galadril_vision.telemetry.context import inject_trace_context
from galadril_vision.telemetry.metrics import PipelineMetrics
from pydantic import JsonValue


class _Processor:
    """Returns deterministic data for actor contract tests."""

    async def process(self, command: PipelineCommand) -> dict[str, JsonValue]:
        await asyncio.sleep(0)
        return {"processed": True, "step": command.step}


class _RemoteMethod:
    """Adapts the local actor to the Ray remote method shape."""

    def __init__(self, actor: PipelineActor) -> None:
        self._actor = actor

    def remote(
        self, command_json: bytes, carrier: Mapping[str, str]
    ) -> Coroutine[object, object, bytes]:
        return self._actor.execute(command_json, carrier)


class _ActorHandle:
    """Provides the dispatcher with a test actor handle."""

    def __init__(self) -> None:
        self.execute = _RemoteMethod(PipelineActor(_Processor()))


def _command() -> PipelineCommand:
    """Builds a valid GPU command fixture."""
    return PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        entity_id="entity-7",
        step="infer",
        step_type=StepType.INFERENCE,
        resource_class=ResourceClass.GPU,
    )


@pytest.mark.anyio
async def test_dispatch_awaits_actor_without_blocking_loop() -> None:
    """Proves another coroutine progresses while the remote task is pending."""
    dispatcher = RayActorDispatcher(
        {ResourceClass.GPU: [_ActorHandle()]},
        PipelineMetrics(),
    )
    loop_progressed = False

    async def mark_progress() -> None:
        nonlocal loop_progressed
        await asyncio.sleep(0)
        loop_progressed = True

    marker = asyncio.create_task(mark_progress())
    result = await dispatcher.dispatch(_command())
    await marker

    assert result.output == {"processed": True, "step": "infer"}
    assert loop_progressed is True


@pytest.mark.anyio
async def test_dispatch_preserves_safe_worker_failure_across_ray() -> None:
    """Checks the wrapper used for errors returned across process boundaries."""
    from galadril_vision.application.errors import CommandProcessingError
    from ray.exceptions import RayTaskError

    cause = CommandProcessingError(
        "private worker details",
        code=ErrorCode.UNAVAILABLE,
        phase=VisionPhase.HANDLER,
    )

    class FailedActor:
        class Method:
            async def remote(
                self, _command_json: bytes, _carrier: Mapping[str, str]
            ) -> bytes:
                raise RayTaskError(
                    "execute",
                    "private traceback",
                    cause,
                )

        execute = Method()

    dispatcher = RayActorDispatcher(
        {ResourceClass.GPU: [FailedActor()]}, PipelineMetrics()
    )
    with pytest.raises(CommandProcessingError) as failure:
        await dispatcher.dispatch(_command())
    assert failure.value.failure == cause.failure
    assert str(failure.value) == "unavailable"


@pytest.mark.anyio
async def test_actor_returns_safe_failing_result() -> None:
    """Rejects accidental exception serialization in the actor's return value."""
    from galadril_vision.application.errors import CommandProcessingError

    cause = CommandProcessingError(
        "private receipt details",
        code=ErrorCode.ACCESS_DENIED,
        phase=VisionPhase.RUNTIME_OPEN,
    )

    class RejectingProcessor:
        async def process(
            self, _command: PipelineCommand
        ) -> dict[str, JsonValue]:
            raise cause

    actor = PipelineActor(RejectingProcessor())
    payload = await actor.execute(_command().model_dump_json().encode(), {})
    assert b"private receipt" not in payload
    result = StepResult.model_validate_json(payload)
    assert result.status is EventStatus.FAILED
    assert result.failure == cause.failure


@pytest.mark.anyio
async def test_actor_requires_valid_w3c_parent_carrier() -> None:
    """Ensures actor execution accepts the explicit serialized W3C boundary."""
    actor = PipelineActor(_Processor())
    command = _command()

    payload = await actor.execute(
        command.model_dump_json().encode("utf-8"), inject_trace_context()
    )

    assert b'"status":"completed"' in payload


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
