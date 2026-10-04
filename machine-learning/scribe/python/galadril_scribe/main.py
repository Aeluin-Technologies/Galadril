"""Standalone private runtime with pooled HTTP and metadata-only OTLP telemetry."""

import asyncio
import logging

import httpx
import structlog
import uvicorn
import uvloop
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from galadril_scribe.contracts import Settings
from galadril_scribe.runtime import Runtime, create_app


async def serve() -> None:
    """Keep transport resources process-scoped and flush exporters on shutdown."""
    settings = Settings()
    logging.basicConfig(level=logging.INFO)
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    resource = Resource.create({"service.name": "galadril-scribe"})
    tracing = TracerProvider(resource=resource)
    tracing.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    metering = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    trace.set_tracer_provider(tracing)
    metrics.set_meter_provider(metering)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(600, connect=10),
            limits=httpx.Limits(
                max_connections=64, max_keepalive_connections=32
            ),
            follow_redirects=False,
        ) as client:
            app = create_app(Runtime(settings, client))
            server = uvicorn.Server(
                uvicorn.Config(
                    app, host="127.0.0.1", port=8091, log_config=None
                )
            )
            await server.serve()
    finally:
        await asyncio.to_thread(tracing.shutdown)
        await asyncio.to_thread(metering.shutdown)


if __name__ == "__main__":
    uvloop.run(serve())
