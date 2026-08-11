"""Structured logging configuration for OTLP integration."""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from structlog.types import EventDict, Processor

_OTEL_LOGGING_INSTRUMENTOR = LoggingInstrumentor()
_OTEL_LOGGING_CONFIGURED = False


class OTLPContextProcessor:
    """Injects active OpenTelemetry trace context into structlog logs."""

    __slots__ = ()

    def __call__(
        self, _logger: Any, _method_name: str, event_dict: EventDict
    ) -> EventDict:
        from galadril_vision.telemetry.context import (
            current_trace_identifiers,
        )

        trace_id, span_id = current_trace_identifiers()
        if trace_id is not None and span_id is not None:
            event_dict["trace_id"] = trace_id
            event_dict["span_id"] = span_id
        return event_dict


class TelemetryConsoleHandler(logging.StreamHandler[TextIO]):
    """Handler for local console output configuration."""


def configure_logging(
    default_level: str = "INFO",
    enable_json_format: bool = False,
    otlp_logger_provider: Any | None = None,
) -> None:
    """Configures structlog and standard logging.

    Args:
        default_level: String representation of the logging level.
        enable_json_format: Force JSON output if True.
        otlp_logger_provider: Optional OTLP provider for log exportation.
    """
    global _OTEL_LOGGING_CONFIGURED

    log_level = getattr(logging, default_level.upper(), logging.INFO)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        OTLPContextProcessor(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.processors.JSONRenderer()
        if enable_json_format or not sys.stdout.isatty()
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[renderer],
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    has_telemetry_handler = False
    for handler in list(root_logger.handlers):
        if isinstance(handler, TelemetryConsoleHandler):
            handler.setFormatter(formatter)
            handler.setLevel(log_level)
            has_telemetry_handler = True

    if not has_telemetry_handler:
        console_handler = TelemetryConsoleHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)
        root_logger.addHandler(console_handler)

    if otlp_logger_provider:
        if not _OTEL_LOGGING_CONFIGURED:
            _OTEL_LOGGING_INSTRUMENTOR.instrument(
                inject_trace_context=True,
                log_handler_level=log_level,
            )
            _OTEL_LOGGING_CONFIGURED = True
