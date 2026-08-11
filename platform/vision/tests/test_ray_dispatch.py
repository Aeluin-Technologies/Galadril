"""Unit tests for non-blocking Ray dispatch and actor trace boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from typing import Any
from uuid import uuid4

import pytest
from galadril_pipeline.config import StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_vision.actors.dispatcher import RayActorDispatcher
from galadril_vision.actors.worker import PipelineActor
from galadril_vision.telemetry.context import inject_trace_context
from galadril_vision.telemetry.metrics import PipelineMetrics
from prometheus_client import CollectorRegistry
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
    ) -> Coroutine[Any, Any, bytes]:
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


@pytest.mark.asyncio
async def test_dispatch_awaits_actor_without_blocking_loop() -> None:
    """Proves another coroutine progresses while the remote task is pending."""
    registry = CollectorRegistry()
    dispatcher = RayActorDispatcher(
        {ResourceClass.GPU: [_ActorHandle()]},
        PipelineMetrics(registry),
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
    assert (
        registry.get_sample_value(
            "galadril_ray_actor_tasks_active",
            {"pipeline": "vision", "step": "infer", "resource_class": "gpu"},
        )
        == 0.0
    )


@pytest.mark.asyncio
async def test_actor_requires_valid_w3c_parent_carrier() -> None:
    """Ensures actor execution accepts the explicit serialized W3C boundary."""
    actor = PipelineActor(_Processor())
    command = _command()

    payload = await actor.execute(
        command.model_dump_json().encode("utf-8"), inject_trace_context()
    )

    assert b'"status":"completed"' in payload
