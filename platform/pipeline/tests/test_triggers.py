import pytest
from datetime import datetime, timezone

from galadril_pipeline.models.pipeline import PipelineStep, StepParams  # type:ignore
from galadril_pipeline.triggers import (  # type:ignore
    get_trigger_spec,
    steps_triggered_on_completion,
)
from galadril_pipeline.triggers import cron_steps_due  # type:ignore


def test_get_trigger_spec_cron_requires_cron() -> None:
    step = PipelineStep(
        step="causal_job",
        type="causal",
        input_from=[],
        params=StepParams(trigger="cron"),
    )
    with pytest.raises(ValueError):
        get_trigger_spec(step)


def test_get_trigger_spec_on_step_requires_on_step() -> None:
    step = PipelineStep(
        step="causal_job",
        type="causal",
        input_from=[],
        params=StepParams(trigger="on_step_completed"),
    )
    with pytest.raises(ValueError):
        get_trigger_spec(step)


def test_steps_triggered_on_completion() -> None:
    s1 = PipelineStep(step="sink", type="sink", input_from=["inference"])
    s2 = PipelineStep(
        step="causal_after_sink",
        type="causal",
        input_from=["sink"],
        params=StepParams(trigger="on_step_completed", on_step="sink"),
    )
    s3 = PipelineStep(
        step="causal_other",
        type="causal",
        input_from=["sink"],
        params=StepParams(trigger="on_step_completed", on_step="resolve"),
    )

    fired = steps_triggered_on_completion([s1, s2, s3], completed_step="sink")
    assert [s.step for s in fired] == ["causal_after_sink"]


@pytest.mark.skipif(
    True, reason="Enable when croniter is installed in dev deps."
)
def test_cron_steps_due_minutely() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    step = PipelineStep(
        step="causal_cron",
        type="causal",
        input_from=["sink"],
        params=StepParams(trigger="cron", cron="*/1 * * * *"),
    )
    due = cron_steps_due([step], now)
    assert [s.step for s in due] == ["causal_cron"]
