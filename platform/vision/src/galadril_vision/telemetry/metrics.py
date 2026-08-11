"""Bounded-cardinality OpenTelemetry pipeline instruments."""

from __future__ import annotations

from collections.abc import Mapping

from opentelemetry import metrics


class PipelineMetrics:
    """Records operational metrics for periodic OTLP export."""

    __slots__ = (
        "_otel_active_ray",
        "_otel_latency",
        "_otel_messages",
        "_otel_ray_tasks",
    )

    def __init__(self) -> None:
        """Creates instruments with low-cardinality pipeline dimensions only."""
        meter = metrics.get_meter("galadril.pipeline")
        self._otel_messages = meter.create_counter(
            "galadril.pipeline.messages",
            description="Messages completed by a pipeline step.",
        )
        self._otel_latency = meter.create_histogram(
            "galadril.pipeline.processing.duration",
            unit="s",
            description="End-to-end processing latency for a pipeline step.",
        )
        self._otel_ray_tasks = meter.create_counter(
            "galadril.ray.actor.tasks",
            description="Ray actor tasks completed by resource pool.",
        )
        self._otel_active_ray = meter.create_up_down_counter(
            "galadril.ray.actor.tasks.active",
            description="Ray actor tasks currently in flight.",
        )

    def message_completed(
        self,
        *,
        pipeline: str,
        step: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        """Records throughput and latency without payload identifiers as labels."""
        labels = {"pipeline": pipeline, "step": step, "outcome": outcome}
        self._otel_messages.add(1, labels)
        self._otel_latency.record(duration_seconds, labels)

    def ray_task_started(
        self, *, pipeline: str, step: str, resource_class: str
    ) -> None:
        """Increments the active Ray task gauges before remote dispatch."""
        labels = _ray_labels(pipeline, step, resource_class)
        self._otel_active_ray.add(1, labels)

    def ray_task_completed(
        self,
        *,
        pipeline: str,
        step: str,
        resource_class: str,
        outcome: str,
    ) -> None:
        """Decrements active work and increments its terminal outcome."""
        labels = _ray_labels(pipeline, step, resource_class)
        self._otel_active_ray.add(-1, labels)
        terminal_labels = {**labels, "outcome": outcome}
        self._otel_ray_tasks.add(1, terminal_labels)


def _ray_labels(
    pipeline: str, step: str, resource_class: str
) -> Mapping[str, str]:
    """Builds the bounded label set shared by Ray instruments."""
    return {
        "pipeline": pipeline,
        "step": step,
        "resource_class": resource_class,
    }
