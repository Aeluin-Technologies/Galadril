"""Unified telemetry framework for logging, metrics, and tracing."""

from __future__ import annotations

from galadril_vision.telemetry.logging import configure_logging
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
]
