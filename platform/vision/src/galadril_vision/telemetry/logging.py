"""Structured logging configuration for OTLP integration."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor


class OTLPContextProcessor:
    """Injects active OpenTelemetry trace context into structlog logs."""

    __slots__ = ()

    def __call__(
        self, _logger: Any, _method_name: str, event_dict: EventDict
    ) -> EventDict:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span and span.is_recording():
            ctx = span.get_span_context()
            if ctx.is_valid:
                event_dict["trace_id"] = f"{ctx.trace_id:032x}"
                event_dict["span_id"] = f"{ctx.span_id:016x}"
        return event_dict


class TelemetryConsoleHandler(logging.StreamHandler):
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
        from opentelemetry.sdk._logs import LoggingHandler

        if not any(isinstance(h, LoggingHandler) for h in root_logger.handlers):
            otlp_handler = LoggingHandler(
                level=log_level, logger_provider=otlp_logger_provider
            )
            root_logger.addHandler(otlp_handler)
