"""CLI spans contain operation names only; credentials never become telemetry attributes."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace


@contextmanager
def operation(name: str) -> Iterator[None]:
    provider = None
    endpoint = os.environ.get("GALADRIL_DEV_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": "galadril-dev"})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=2))
        )
        tracer = provider.get_tracer("galadril.dev")
    else:
        tracer = trace.get_tracer("galadril.dev")
    try:
        with tracer.start_as_current_span(
            "dev." + name, record_exception=False, set_status_on_exception=False
        ):
            yield
    finally:
        if provider is not None:
            provider.shutdown()
