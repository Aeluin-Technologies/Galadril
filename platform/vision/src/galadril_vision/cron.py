from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from galadril_pipeline.triggers import cron_steps_due  # type: ignore
from galadril_vision.causal.runner import (
    AmarthCausalRunner,
    build_slice_spec_from_step_params,
)

logger = structlog.get_logger(__name__)


async def run_due_causal_cron_steps(
    *,
    pipeline_steps: list[Any],
    runner: AmarthCausalRunner,
    now: datetime | None = None,
) -> None:
    ts = now or datetime.now(timezone.utc)
    due = cron_steps_due(pipeline_steps, ts)

    for step in due:
        if getattr(step, "type", None) != "causal" or not getattr(
            step, "params", None
        ):
            continue

        params = step.params
        target_outcome = (
            params.amarth_target_outcome or "state_avg_confidence.sighting"
        )
        window_size = params.amarth_window_size

        targets = getattr(params, "targets", None)
        if isinstance(targets, list) and targets:
            target_list = [
                t for t in targets if isinstance(t, str) and t.strip()
            ]
        else:
            target_list = [str(getattr(params, "target", "entity:")).strip()]

        for target in target_list:
            params.target = target
            spec = build_slice_spec_from_step_params(params)

            logger.info(
                "causal_cron_job_started",
                step=step.step,
                cron=params.cron,
                target=spec.target,
                target_outcome=target_outcome,
            )

            result = await runner.run(
                spec=spec,
                target_outcome=target_outcome,
                window_size=window_size,
            )

            logger.info(
                "causal_cron_job_completed",
                step=step.step,
                target=spec.target,
                status=result.get("status", "unknown"),
                reason=result.get("reason"),
                persisted_edges=result.get("persisted_edges"),
                cache_key=result.get("cache_key"),
            )
