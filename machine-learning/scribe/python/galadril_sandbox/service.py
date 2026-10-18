"""Private local execution never exposes the Docker API to Scribe or code."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
import uvicorn
import uvloop
from fastapi import FastAPI, HTTPException, Request
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

from galadril_sandbox.contracts import ExecutionRequest, ExecutionResult
from galadril_sandbox.controller import (
    Controller,
    ControllerSettings,
    negotiate_engine,
    private_listener,
)

logger = structlog.get_logger(__name__)


async def reap(controller: Controller) -> None:
    """Recover controller crashes without ever inspecting unrelated containers."""
    while True:
        try:
            response = await controller.engine.get(
                "containers/json",
                params={
                    "all": "true",
                    "filters": json.dumps(
                        {"label": ["galadril.python-sandbox=true"]}
                    ),
                },
            )
            response.raise_for_status()
            items: object = response.json()
            if not isinstance(items, list):
                raise ValueError("Invalid container inventory")
            for item in items[:128]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("Created"), int)
                    or item["Created"] > time.time() - 120
                ):
                    continue
                identifier = item.get("Id")
                if (
                    not isinstance(identifier, str)
                    or len(identifier) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in identifier
                    )
                ):
                    continue
                removed = await controller.engine.delete(
                    f"containers/{identifier}",
                    params={"force": "true", "v": "true"},
                )
                if removed.status_code not in {204, 404}:
                    logger.error(
                        "sandbox.reap.rejected", status_code=removed.status_code
                    )
        except (httpx.HTTPError, ValueError):
            logger.error("sandbox.reap.unavailable")
        await asyncio.sleep(60)


def create_app(controller: Controller) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cleanup = asyncio.create_task(reap(controller))
        try:
            yield
        finally:
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/execute")
    async def execute(incoming: Request) -> ExecutionResult:
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                async for part in incoming.stream():
                    if len(body) + len(part) > 65536:
                        raise HTTPException(413, "Execution exceeds limit")
                    body.extend(part)
            request = ExecutionRequest.model_validate_json(body)
        except ValueError:
            raise HTTPException(422, "Execution rejected") from None
        except TimeoutError:
            raise HTTPException(408, "Execution timed out") from None
        try:
            return await controller.execute(request)
        except RuntimeError:
            raise HTTPException(429, "Sandbox capacity exceeded") from None

    return app


async def serve() -> None:
    settings = ControllerSettings()
    logging.basicConfig(level=logging.INFO)
    structlog.configure(
        logger_factory=structlog.stdlib.LoggerFactory(),
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )
    resource = Resource.create({"service.name": "galadril-python-sandbox"})
    tracing = TracerProvider(resource=resource)
    tracing.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracing)
    metering = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    metrics.set_meter_provider(metering)
    logs = LoggerProvider(resource=resource)
    logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    logging.getLogger("galadril_sandbox").addHandler(
        LoggingHandler(level=logging.INFO, logger_provider=logs)
    )
    try:
        transport = httpx.AsyncHTTPTransport(uds=settings.docker_socket)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://docker/v1.47/",
            trust_env=False,
            timeout=65,
        ) as engine:
            await negotiate_engine(engine)
            with private_listener(settings.socket_path) as listener:
                try:
                    await uvicorn.Server(
                        uvicorn.Config(
                            create_app(Controller(settings, engine)),
                            log_config=None,
                            access_log=False,
                            limit_concurrency=16,
                        )
                    ).serve(sockets=[listener])
                finally:
                    Path(settings.socket_path).unlink(missing_ok=True)
    finally:
        await asyncio.to_thread(tracing.shutdown)
        await asyncio.to_thread(metering.shutdown)
        await asyncio.to_thread(logs.shutdown)


if __name__ == "__main__":
    uvloop.run(serve())
