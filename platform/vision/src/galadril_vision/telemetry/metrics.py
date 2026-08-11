"""Bounded-cardinality Prometheus and OpenTelemetry pipeline instruments."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from opentelemetry import metrics
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
)


class PipelineMetrics:
    """Records matching operational metrics in Prometheus and OTLP pipelines."""

    __slots__ = (
        "_active_ray",
        "_messages",
        "_otel_active_ray",
        "_otel_latency",
        "_otel_messages",
        "_otel_ray_tasks",
        "_processing_latency",
        "_ray_tasks",
    )

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        """Creates instruments with low-cardinality pipeline dimensions only."""
        self._messages = Counter(
            "galadril_pipeline_messages_total",
            "Messages completed by a pipeline step.",
            ("pipeline", "step", "outcome"),
            registry=registry,
        )
        self._processing_latency = Histogram(
            "galadril_pipeline_processing_duration_seconds",
            "End-to-end processing latency for a pipeline step.",
            ("pipeline", "step", "outcome"),
            buckets=_LATENCY_BUCKETS,
            registry=registry,
        )
        self._ray_tasks = Counter(
            "galadril_ray_actor_tasks_total",
            "Ray actor tasks completed by resource pool.",
            ("pipeline", "step", "resource_class", "outcome"),
            registry=registry,
        )
        self._active_ray = Gauge(
            "galadril_ray_actor_tasks_active",
            "Ray actor tasks currently in flight.",
            ("pipeline", "step", "resource_class"),
            registry=registry,
        )

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
        self._messages.labels(**labels).inc()
        self._processing_latency.labels(**labels).observe(duration_seconds)
        self._otel_messages.add(1, labels)
        self._otel_latency.record(duration_seconds, labels)

    def ray_task_started(
        self, *, pipeline: str, step: str, resource_class: str
    ) -> None:
        """Increments the active Ray task gauges before remote dispatch."""
        labels = _ray_labels(pipeline, step, resource_class)
        self._active_ray.labels(**labels).inc()
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
        self._active_ray.labels(**labels).dec()
        self._otel_active_ray.add(-1, labels)
        terminal_labels = {**labels, "outcome": outcome}
        self._ray_tasks.labels(**terminal_labels).inc()
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
