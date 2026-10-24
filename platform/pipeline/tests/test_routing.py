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


def test_source_roundtrip_preserves_parser_semantics_and_ingestion_selectors() -> (
    None
):
    source = Source.model_validate(
        {
            "id": "mail",
            "topic": "raw",
            "match_pattern": ".*",
            "schema_path": "text.avsc",
            "parser": "text",
            "source_kind": "email",
            "group_id": "email",
            "content_types": ["text/plain"],
        }
    )
    assert source.parser == "text"
    assert source.source_kind == "email"
    assert source.model_dump()["group_id"] == "email"


def _branch_config() -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "name": "mail",
            "sources": [_source().model_dump()],
            "pipeline": [
                {
                    "step": "classify",
                    "type": "route",
                    "input_from": ["raw"],
                    "routing": "regex",
                    "branches": {"legal": ["law"], "other": ["archive"]},
                },
                {"step": "law", "type": "sink", "input_from": ["classify"]},
                {"step": "archive", "type": "sink", "input_from": ["classify"]},
            ],
        }
    )


@pytest.mark.parametrize("strategy", ["regex", "slm"])
def test_route_strategy_is_native_and_does_not_require_a_plugin(
    strategy: str,
) -> None:
    step = PipelineStep.model_validate(
        {
            "step": "choose",
            "type": "route",
            "routing": strategy,
            "input_from": ["raw"],
            "branches": {"chosen": ["sink"]},
        }
    )
    assert step.capability_id is None
    assert step.model_dump(mode="json")["routing"] == strategy


@pytest.mark.parametrize(
    "update",
    [
        {"routing": "unknown"},
        {"routing": None},
        {"capability_id": "routing.route.regex"},
        {"type": "sink", "branches": {}},
    ],
)
def test_route_configuration_cannot_be_reinterpreted_as_a_plugin(
    update: dict[str, object],
) -> None:
    definition = {
        "step": "choose",
        "type": "route",
        "routing": "regex",
        "input_from": ["raw"],
        "branches": {"chosen": ["sink"]},
    }
    with pytest.raises(ValueError):
        PipelineStep.model_validate(definition | update)


def test_selected_branches_never_fan_out_to_unselected_destinations() -> None:
    routes = PipelineRouteTable(_branch_config())
    assert routes.successors("classify", {"branch": "legal"}) == ("law",)
    assert routes.successors("classify", {"branch": "other"}) == ("archive",)
    with pytest.raises(RouteCompilationError):
        routes.successors("classify", {"branch": "injected"})
    with pytest.raises(RouteCompilationError):
        routes.successors("classify", {})


@pytest.mark.parametrize("targets", [["missing"], ["raw"], []])
def test_branch_destinations_must_be_declared_immediate_consumers(
    targets: list[str],
) -> None:
    config = _branch_config()
    step = config.pipeline[0]
    step.branches["legal"] = targets
    with pytest.raises(RouteCompilationError):
        PipelineRouteTable(config)


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


def test_route_table_keeps_on_demand_causal_work_off_the_stream() -> None:
    """A causal command must wait for the completed entity history."""
    config = PipelineConfig(
        name="vision",
        sources=[_source()],
        pipeline=[
            PipelineStep(
                step="sink",
                type=StepType.SINK,
                input_from=["raw"],
            ),
            PipelineStep(
                step="causal",
                type=StepType.CAUSAL,
                input_from=["sink"],
                params=StepParams(trigger=TriggerType.ON_DEMAND),
            ),
        ],
    )

    routes = PipelineRouteTable(config)

    assert routes.route("sink").downstream == ()
    assert routes.route("causal").resource_class is ResourceClass.CAUSAL
    assert routes.route("causal").scheduled is True
    assert routes.scheduled_steps == ()


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
