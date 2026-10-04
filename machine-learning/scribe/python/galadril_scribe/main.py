"""Standalone private runtime with pooled HTTP and metadata-only OTLP telemetry."""

import asyncio
import logging

import httpx
import structlog
import uvicorn
import uvloop
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from galadril_scribe.contracts import Settings
from galadril_scribe.runtime import Runtime, create_app
from galadril_scribe.sandbox import Sandbox


async def serve() -> None:
    """Keep transport resources process-scoped and flush exporters on shutdown."""
    settings = Settings()
    logging.basicConfig(level=logging.WARNING)
    structlog.configure(
        logger_factory=structlog.stdlib.LoggerFactory(),
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )
    resource = Resource.create({"service.name": "galadril-scribe"})
    tracing = TracerProvider(resource=resource)
    tracing.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    metering = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    trace.set_tracer_provider(tracing)
    metrics.set_meter_provider(metering)
    logging_provider = LoggerProvider(resource=resource)
    logging_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter())
    )
    service_logger = logging.getLogger("galadril_scribe")
    service_logger.setLevel(logging.INFO)
    service_logger.propagate = False
    service_logger.addHandler(logging.StreamHandler())
    service_logger.addHandler(
        LoggingHandler(level=logging.INFO, logger_provider=logging_provider)
    )
    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(600, connect=10),
                limits=httpx.Limits(
                    max_connections=64, max_keepalive_connections=32
                ),
                follow_redirects=False,
                trust_env=False,
            ) as client,
            Sandbox() as sandbox,
        ):
            app = create_app(Runtime(settings, client, sandbox=sandbox))
            server = uvicorn.Server(
                uvicorn.Config(
                    app, host="127.0.0.1", port=8091, log_config=None
                )
            )
            await server.serve()
    finally:
        await asyncio.to_thread(tracing.shutdown)
        await asyncio.to_thread(metering.shutdown)
        await asyncio.to_thread(logging_provider.shutdown)


if __name__ == "__main__":
    uvloop.run(serve())
