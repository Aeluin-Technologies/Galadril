"""Non-blocking structlog bridge to the OpenTelemetry logs signal."""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from structlog.types import EventDict, Processor

_EVENT_SEPARATOR = re.compile(r"[^a-z0-9]+")
_LOG_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__)
_LOGGING_INSTRUMENTOR = LoggingInstrumentor()


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


class EventSchemaProcessor:
    """Normalizes structured records to the cross-language event contract."""

    __slots__ = ()

    def __call__(
        self, _logger: Any, _method_name: str, event_dict: EventDict
    ) -> EventDict:
        raw_event = str(event_dict.get("event", "log.record")).strip().lower()
        identifier = _event_identifier(raw_event)
        message = str(event_dict.get("message", raw_event)).strip().lower()
        if message == raw_event:
            message = _EVENT_SEPARATOR.sub(" ", raw_event).strip()

        normalized: EventDict = {
            str(key).lower(): value
            for key, value in event_dict.items()
            if key not in {"event", "message"}
        }
        normalized["event.name"] = identifier
        normalized["event"] = message or "log record"
        return normalized


class EventSchemaFilter(logging.Filter):
    """Adds the event contract to logs emitted by standard-library clients."""

    def filter(self, record: logging.LogRecord) -> bool:
        phrase = record.getMessage().strip().lower()
        record.msg = _EVENT_SEPARATOR.sub(" ", phrase).strip() or "log record"
        record.args = ()
        for key in tuple(record.__dict__):
            lowered = key.lower()
            if (
                key not in _LOG_RECORD_KEYS
                and key != lowered
                and lowered not in record.__dict__
            ):
                record.__dict__[lowered] = record.__dict__.pop(key)
        record.__dict__.setdefault("event.name", _event_identifier(phrase))
        return True


def _event_identifier(phrase: str) -> str:
    """Builds a stable lowercase dot identifier from an event phrase."""
    tokens = [token for token in _EVENT_SEPARATOR.split(phrase) if token]
    if len(tokens) < 2:
        tokens.insert(0, "log")
    return ".".join(tokens)


def configure_logging(
    default_level: str = "INFO",
    enable_json_format: bool = False,
    otlp_logger_provider: LoggerProvider | None = None,
) -> None:
    """Routes structlog and standard logging to the OTLP batch processor.

    Args:
        default_level: String representation of the logging level.
        enable_json_format: Retained for caller compatibility; OTLP is structured.
        otlp_logger_provider: Optional OTLP logger provider.
    """
    del enable_json_format
    log_level = getattr(logging, default_level.upper(), logging.INFO)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        OTLPContextProcessor(),
        EventSchemaProcessor(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.render_to_log_kwargs,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    if _LOGGING_INSTRUMENTOR.is_instrumented_by_opentelemetry:
        _LOGGING_INSTRUMENTOR.uninstrument()
    root_logger.handlers.clear()

    if otlp_logger_provider is None:
        root_logger.addHandler(logging.NullHandler())
    else:
        _LOGGING_INSTRUMENTOR.instrument(
            inject_trace_context=True,
            log_handler_level=log_level,
        )
        for handler in root_logger.handlers:
            handler.addFilter(EventSchemaFilter())


def shutdown_logging() -> None:
    """Removes the OpenTelemetry handler and restores the record factory."""
    if _LOGGING_INSTRUMENTOR.is_instrumented_by_opentelemetry:
        _LOGGING_INSTRUMENTOR.uninstrument()
