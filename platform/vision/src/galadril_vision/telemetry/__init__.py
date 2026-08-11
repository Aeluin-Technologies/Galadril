"""Unified telemetry framework for logging, metrics, and tracing."""

from __future__ import annotations

from galadril_vision.telemetry.context import (
    bind_pipeline_context,
    extract_trace_context,
    inject_trace_context,
    start_span_from_carrier,
)
from galadril_vision.telemetry.logging import configure_logging
from galadril_vision.telemetry.metrics import PipelineMetrics
from galadril_vision.telemetry.tracing import (
    TraceContext,
    configure_telemetry,
    flush_telemetry,
    instrument,
    shutdown_telemetry,
)

__all__ = [
    "configure_logging",
    "configure_telemetry",
    "flush_telemetry",
    "shutdown_telemetry",
    "instrument",
    "TraceContext",
    "PipelineMetrics",
    "bind_pipeline_context",
    "extract_trace_context",
    "inject_trace_context",
    "start_span_from_carrier",
]
