"""Unit tests for deterministic streaming route compilation."""

import pytest
from galadril_pipeline.config import (
    PipelineConfig,
    PipelineStep,
    Source,
    StepParams,
    StepType,
    TriggerType,
)
from galadril_pipeline.events import ResourceClass
from galadril_pipeline.routing import PipelineRouteTable, RouteCompilationError


def _source(identifier: str = "raw") -> Source:
    """Builds a compact valid source fixture."""
    return Source(
        id=identifier,
        topic="raw",
        match_pattern=".*",
        schema_path="schema.avsc",
    )


def test_route_table_separates_event_and_timer_edges() -> None:
    """Cron steps must start from timers rather than every upstream event."""
    config = PipelineConfig(
        name="vision",
        sources=[_source()],
        pipeline=[
            PipelineStep(
                step="infer",
                type=StepType.INFERENCE,
                model="models.FaceModel",
                input_from=["raw"],
            ),
            PipelineStep(
                step="sink",
                type=StepType.SINK,
                input_from=["infer"],
            ),
            PipelineStep(
                step="causal",
                type=StepType.CAUSAL,
                input_from=["sink"],
                params=StepParams(
                    trigger=TriggerType.CRON,
                    cron="0 * * * *",
                ),
            ),
        ],
    )

    routes = PipelineRouteTable(config)

    assert routes.entry_steps("raw") == ("infer",)
    assert routes.route("infer").resource_class is ResourceClass.GPU
    assert routes.route("infer").downstream == ("sink",)
    assert routes.route("sink").downstream == ()
    assert routes.scheduled_steps == ("causal",)


def test_route_table_rejects_implicit_streaming_join() -> None:
    """Requires stateful join semantics rather than racing two input events."""
    config = PipelineConfig(
        name="join",
        sources=[_source("left"), _source("right")],
        pipeline=[
            PipelineStep(
                step="sink",
                type=StepType.SINK,
                input_from=["left", "right"],
            )
        ],
    )

    with pytest.raises(RouteCompilationError, match="explicit stateful join"):
        PipelineRouteTable(config)
