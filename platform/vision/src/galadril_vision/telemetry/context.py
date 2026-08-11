"""W3C trace propagation and structured pipeline log context helpers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager

import structlog
from opentelemetry import context, trace
from opentelemetry.trace import Span, SpanKind
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

type TraceCarrier = dict[str, str]
_PROPAGATOR = TraceContextTextMapPropagator()


def inject_trace_context(
    carrier: MutableMapping[str, str] | None = None,
    otel_context: context.Context | None = None,
) -> TraceCarrier:
    """Serializes the active W3C trace context for a process boundary."""
    target: MutableMapping[str, str] = carrier if carrier is not None else {}
    _PROPAGATOR.inject(target, context=otel_context)
    return dict(target)


def extract_trace_context(
    carrier: Mapping[str, str | bytes],
) -> context.Context:
    """Extracts a remote W3C parent from Kafka or Ray-safe string headers."""
    normalized = {
        key.lower(): value.decode("ascii")
        if isinstance(value, bytes)
        else value
        for key, value in carrier.items()
    }
    return _PROPAGATOR.extract(carrier=normalized)


def current_trace_identifiers() -> tuple[str | None, str | None]:
    """Returns fixed-width identifiers for the current valid span context."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None, None
    return f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}"


@contextmanager
def start_span_from_carrier(
    name: str,
    carrier: Mapping[str, str | bytes],
    *,
    kind: SpanKind = SpanKind.CONSUMER,
    attributes: Mapping[str, str | int | float | bool] | None = None,
) -> Iterator[Span]:
    """Starts a child span whose remote parent retains the incoming Trace ID."""
    remote_context = extract_trace_context(carrier)
    tracer = trace.get_tracer("galadril.pipeline")
    with tracer.start_as_current_span(
        name,
        context=remote_context,
        kind=kind,
        attributes=attributes,
    ) as span:
        yield span


@contextmanager
def bind_pipeline_context(
    *,
    pipeline: str,
    step: str,
    entity_id: str | None,
) -> Iterator[None]:
    """Binds stable payload coordinates to every structured log in scope."""
    with structlog.contextvars.bound_contextvars(
        pipeline=pipeline,
        step=step,
        entity_id=entity_id,
    ):
        yield
