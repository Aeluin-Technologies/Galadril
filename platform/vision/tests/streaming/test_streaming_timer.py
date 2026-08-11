"""Unit tests for deterministic cron command generation."""

from datetime import UTC, datetime

from galadril_pipeline.config import (
    PipelineConfig,
    PipelineStep,
    Source,
    StepParams,
    StepType,
    TriggerType,
)
from galadril_pipeline.events import ResourceClass
from galadril_pipeline.routing import PipelineRouteTable
from galadril_vision.streaming.timer import ScheduledCommandFactory


def test_scheduled_targets_have_stable_distinct_command_ids() -> None:
    """Ensures timer replay is idempotent while each target remains distinct."""
    config = PipelineConfig(
        name="vision",
        sources=[
            Source(
                id="raw",
                topic="raw",
                match_pattern=".*",
                schema_path="schema.avsc",
            )
        ],
        pipeline=[
            PipelineStep(
                step="causal",
                type=StepType.CAUSAL,
                input_from=["raw"],
                params=StepParams(
                    trigger=TriggerType.CRON,
                    cron="0 * * * *",
                    targets=["entity:one", "entity:two"],
                ),
            )
        ],
    )
    factory = ScheduledCommandFactory(config, PipelineRouteTable(config))
    scheduled_for = datetime(2026, 8, 10, 12, tzinfo=UTC)

    first = factory.commands_for(factory.schedules[0], scheduled_for)
    replay = factory.commands_for(factory.schedules[0], scheduled_for)

    assert [command.event_id for command in first] == [
        command.event_id for command in replay
    ]
    assert first[0].event_id != first[1].event_id
    assert first[0].resource_class is ResourceClass.CAUSAL
    assert first[0].payload["target"] == "entity:one"
