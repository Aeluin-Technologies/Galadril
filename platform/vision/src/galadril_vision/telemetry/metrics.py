"""Bounded-cardinality OpenTelemetry pipeline instruments."""

from galadril_pipeline.telemetry_contract import (
    METRIC_VISION_PHASE_DURATION,
    METRIC_VISION_PHASE_DURATION_UNIT,
    METRIC_VISION_STEP_DURATION,
    METRIC_VISION_STEP_DURATION_UNIT,
    METRIC_VISION_STEP_MESSAGES,
    METRIC_VISION_STEP_MESSAGES_UNIT,
    METRIC_VISION_TASK_ACTIVE,
    METRIC_VISION_TASK_ACTIVE_UNIT,
    METRIC_VISION_TASK_COMPLETED,
    METRIC_VISION_TASK_COMPLETED_UNIT,
    VisionOutcome,
    VisionResourceClass,
    VisionTimingPhase,
    vision_phase_duration_attributes,
    vision_step_duration_attributes,
    vision_step_messages_attributes,
    vision_task_active_attributes,
    vision_task_completed_attributes,
)
from opentelemetry import metrics


class PipelineMetrics:
    """Records operational metrics for periodic OTLP export."""

    __slots__ = (
        "_otel_active_ray",
        "_otel_latency",
        "_otel_messages",
        "_otel_phase_duration",
        "_otel_ray_tasks",
    )

    def __init__(self) -> None:
        meter = metrics.get_meter("galadril.pipeline")
        self._otel_messages = meter.create_counter(
            METRIC_VISION_STEP_MESSAGES, unit=METRIC_VISION_STEP_MESSAGES_UNIT
        )
        self._otel_latency = meter.create_histogram(
            METRIC_VISION_STEP_DURATION, unit=METRIC_VISION_STEP_DURATION_UNIT
        )
        self._otel_phase_duration = meter.create_histogram(
            METRIC_VISION_PHASE_DURATION,
            unit=METRIC_VISION_PHASE_DURATION_UNIT,
            explicit_bucket_boundaries_advisory=(
                0.0001,
                0.001,
                0.005,
                0.01,
                0.025,
                0.05,
                0.1,
                0.25,
                0.5,
                1,
                2.5,
                5,
                10,
                30,
                60,
                120,
                300,
            ),
        )
        self._otel_ray_tasks = meter.create_counter(
            METRIC_VISION_TASK_COMPLETED, unit=METRIC_VISION_TASK_COMPLETED_UNIT
        )
        self._otel_active_ray = meter.create_up_down_counter(
            METRIC_VISION_TASK_ACTIVE, unit=METRIC_VISION_TASK_ACTIVE_UNIT
        )

    def message_completed(
        self, *, outcome: VisionOutcome, duration_seconds: float
    ) -> None:
        """Records the bounded outcome without user-defined workflow labels."""
        self._otel_messages.add(
            1, vision_step_messages_attributes(vision_outcome=outcome)
        )
        self._otel_latency.record(
            duration_seconds,
            vision_step_duration_attributes(vision_outcome=outcome),
        )

    def ray_task_started(self, resource_class: VisionResourceClass) -> None:
        self._otel_active_ray.add(
            1,
            vision_task_active_attributes(vision_resource_class=resource_class),
        )

    def ray_task_completed(
        self, resource_class: VisionResourceClass, outcome: VisionOutcome
    ) -> None:
        """Balances the in-flight value even when dispatch was cancelled."""
        self._otel_active_ray.add(
            -1,
            vision_task_active_attributes(vision_resource_class=resource_class),
        )
        self._otel_ray_tasks.add(
            1,
            vision_task_completed_attributes(
                vision_outcome=outcome, vision_resource_class=resource_class
            ),
        )

    def phase_completed(
        self,
        *,
        phase: VisionTimingPhase,
        resource_class: VisionResourceClass,
        duration_seconds: float,
    ) -> None:
        """Records a phase for all traffic with only fixed phase and pool labels."""
        self._otel_phase_duration.record(
            duration_seconds,
            vision_phase_duration_attributes(
                vision_timing_phase=phase, vision_resource_class=resource_class
            ),
        )
