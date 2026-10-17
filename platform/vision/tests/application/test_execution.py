"""Contract tests for infrastructure-independent command execution."""

import asyncio
from uuid import uuid4

import pytest
from galadril_pipeline.config import PipelineStep, StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from galadril_vision.application.execution import (
    CommandProcessingError,
    ExecutionRoute,
    PipelineExecutor,
)
from pydantic import JsonValue


class RecordingHandler:
    def __init__(self, value: str) -> None:
        self.value = value
        self.commands: list[PipelineCommand] = []
        self.steps: list[PipelineStep] = []

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        self.commands.append(command)
        self.steps.append(step)
        return {"backend": self.value, "model": step.model}


def _step() -> PipelineStep:
    return PipelineStep.model_validate(
        {"step": "save", "type": "sink", "input_from": ["source"]}
    )


def _command(
    route: str = "tenant-a/daily/rev1", **updates: object
) -> PipelineCommand:
    return PipelineCommand.model_validate(
        {
            "correlation_id": uuid4(),
            "tenant_id": "tenant-a",
            "pipeline": route,
            "step": "save",
            "step_type": "sink",
            "resource_class": ResourceClass.CPU,
        }
        | updates
    )


def _route(name: str, tenant: str, handler: RecordingHandler) -> ExecutionRoute:
    return ExecutionRoute(
        execution_id=name,
        tenant_id=tenant,
        pipeline_id="daily",
        revision_id="rev1",
        steps=(_step(),),
        handlers={"save": handler},
    )


def test_selects_backend_by_published_route() -> None:
    first = RecordingHandler("postgres")
    second = RecordingHandler("alternative")
    executor = PipelineExecutor(
        (
            _route("tenant-a/daily/rev1", "tenant-a", first),
            _route("tenant-b/daily/rev1", "tenant-b", second),
        )
    )
    result = asyncio.run(
        executor.process(_command("tenant-b/daily/rev1", tenant_id="tenant-b"))
    )
    assert result == {"backend": "alternative", "model": None}
    assert not first.commands
    assert len(second.commands) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"tenant_id": "tenant-b"},
        {"pipeline": "tenant-a/daily/stale"},
        {"step": "unpublished"},
        {"step_type": StepType.RESOLVE},
    ],
)
def test_rejects_invalid_command_before_handler(
    updates: dict[str, object],
) -> None:
    handler = RecordingHandler("unused")
    executor = PipelineExecutor(
        (_route("tenant-a/daily/rev1", "tenant-a", handler),)
    )
    with pytest.raises(CommandProcessingError):
        asyncio.run(executor.process(_command(**updates)))
    assert not handler.commands


def test_snapshots_step_definitions_and_bindings() -> None:
    handler = RecordingHandler("trusted")
    step = _step()
    bindings = {"save": handler}
    route = ExecutionRoute(
        execution_id="tenant-a/daily/rev1",
        tenant_id="tenant-a",
        pipeline_id="daily",
        revision_id="rev1",
        steps=(step,),
        handlers=bindings,
    )
    executor = PipelineExecutor((route,))
    bindings.clear()
    assert step.params.model_extra is not None
    step.params.model_extra["marker"] = "changed"
    result = asyncio.run(executor.process(_command()))
    assert result == {"backend": "trusted", "model": None}
    assert "marker" not in (handler.steps[0].params.model_extra or {})


def test_rejects_duplicate_routes() -> None:
    route = _route(
        "tenant-a/daily/rev1", "tenant-a", RecordingHandler("unused")
    )
    with pytest.raises(ValueError, match="unique"):
        PipelineExecutor((route, route))


@pytest.mark.parametrize(
    "binding", [{}, {"unknown": RecordingHandler("unused")}]
)
def test_requires_exact_handler_bindings(
    binding: dict[str, RecordingHandler],
) -> None:
    with pytest.raises(ValueError, match="bindings"):
        PipelineExecutor(
            (
                ExecutionRoute(
                    "route", "tenant-a", "daily", "rev1", (_step(),), binding
                ),
            )
        )


def test_rejects_duplicate_steps() -> None:
    handler = RecordingHandler("unused")
    with pytest.raises(ValueError, match="unique"):
        PipelineExecutor(
            (
                ExecutionRoute(
                    "route",
                    "tenant-a",
                    "daily",
                    "rev1",
                    (_step(), _step()),
                    {"save": handler},
                ),
            )
        )


def test_requires_at_least_one_route() -> None:
    with pytest.raises(ValueError, match="At least one"):
        PipelineExecutor(())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
