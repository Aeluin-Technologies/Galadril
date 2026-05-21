from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from galadril_pipeline.models.pipeline import PipelineStep


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    step: str
    trigger: str
    cron: Optional[str]
    on_step: Optional[str]


def get_trigger_spec(step: PipelineStep) -> Optional[TriggerSpec]:
    if not step.params or not step.params.trigger:
        return None

    trig = step.params.trigger
    if trig == "cron":
        if not step.params.cron:
            raise ValueError(
                f"Step '{step.step}' trigger=cron requires params.cron"
            )
        return TriggerSpec(
            step=step.step, trigger=trig, cron=step.params.cron, on_step=None
        )

    if trig == "on_step_completed":
        if not step.params.on_step:
            raise ValueError(
                f"Step '{step.step}' trigger=on_step_completed requires params.on_step"
            )
        return TriggerSpec(
            step=step.step, trigger=trig, cron=None, on_step=step.params.on_step
        )

    if trig == "manual":
        return TriggerSpec(
            step=step.step, trigger=trig, cron=None, on_step=None
        )

    raise ValueError(f"Unsupported trigger '{trig}' for step '{step.step}'")


def steps_triggered_on_completion(
    steps: list[PipelineStep], completed_step: str
) -> list[PipelineStep]:
    out: list[PipelineStep] = []
    for s in steps:
        spec = get_trigger_spec(s)
        if (
            spec
            and spec.trigger == "on_step_completed"
            and spec.on_step == completed_step
        ):
            out.append(s)
    return out
