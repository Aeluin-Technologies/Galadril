"""OpenTelemetry instrumentation and lifecycle management."""

from __future__ import annotations

import functools
import inspect
import socket
import threading
import time
from typing import Any, Callable, TypeVar, overload

import structlog
from opentelemetry import context, metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import Link, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

logger = structlog.get_logger(__name__)
F = TypeVar("F", bound=Callable[..., Any])
_PROPAGATOR = TraceContextTextMapPropagator()


class InstrumentRegistry:
    """Central registry for caching metric instruments."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], Any] = {}

    def get_instrument(
        self, kind: str, name: str, description: str, unit: str = ""
    ) -> Any:
        """Retrieves or creates a metric instrument.

        Args:
            kind: Instrument type ("counter" or "histogram").
            name: Metric name.
            description: Instrument description.
            unit: Metric unit.
        """
        key = (kind, name)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

            meter = metrics.get_meter("telemetry.engine")
            if kind == "counter":
                instrument = meter.create_counter(name, description=description)
            elif kind == "histogram":
                instrument = meter.create_histogram(
                    name, unit=unit, description=description
                )
            else:
                raise ValueError(f"Unsupported instrument type: {kind}")

            self._cache[key] = instrument
            return instrument

    def clear(self) -> None:
        """Clears the instrument cache."""
        with self._lock:
            self._cache.clear()


class TelemetryManager:
    """Manages OpenTelemetry provider lifecycles."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tracer_provider: TracerProvider | None = None
        self._meter_provider: MeterProvider | None = None
        self._logger_provider: Any | None = None
        self._is_initialized = False
        self._is_shutdown = False
        self._state_version = 0

    @property
    def state_version(self) -> int:
        """Returns the current configuration version."""
        with self._lock:
            return self._state_version

    def configure(
        self,
        service_name: str,
        environment: str = "production",
        version: str = "1.0.0",
        otlp_endpoint: str | None = None,
    ) -> tuple[TracerProvider | None, MeterProvider | None, Any | None]:
        """Initializes tracing, metrics, and logging pipelines.

        Args:
            service_name: Name of the service.
            environment: Deployment environment.
            version: Service version.
            otlp_endpoint: OTLP collector endpoint. Uses defaults if None.
        """
        with self._lock:
            if self._is_shutdown:
                raise RuntimeError("Telemetry manager is shut down.")
            if self._is_initialized:
                logger.warning("telemetry_manager_already_initialized")
                return (
                    self._tracer_provider,
                    self._meter_provider,
                    self._logger_provider,
                )

            resource = self._build_resource(service_name, environment, version)

            if otlp_endpoint == "console":
                self._setup_console_pipeline(resource)
            else:
                self._setup_otlp_pipeline(resource, otlp_endpoint)

            self._is_initialized = True
            self._state_version += 1
            return (
                self._tracer_provider,
                self._meter_provider,
                self._logger_provider,
            )

    def _build_resource(
        self, service_name: str, environment: str, version: str
    ) -> Resource:
        attributes: dict[str, Any] = {
            "service.name": service_name,
            "deployment.environment": environment,
            "service.version": version,
        }
        try:
            attributes["service.instance.id"] = socket.gethostname()
        except Exception:
            attributes["service.instance.id"] = "unknown-host"
        return Resource.create(attributes=attributes)

    def _setup_console_pipeline(self, resource: Resource) -> None:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            ConsoleLogRecordExporter,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import (
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )

        self._tracer_provider = TracerProvider(resource=resource)
        self._tracer_provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter())
        )
        trace.set_tracer_provider(self._tracer_provider)

        reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(), export_interval_millis=30000
        )
        self._meter_provider = MeterProvider(
            resource=resource, metric_readers=[reader]
        )
        metrics.set_meter_provider(self._meter_provider)

        self._logger_provider = LoggerProvider(resource=resource)
        self._logger_provider.add_log_record_processor(
            SimpleLogRecordProcessor(ConsoleLogRecordExporter())
        )
        from opentelemetry._logs import set_logger_provider

        set_logger_provider(self._logger_provider)

    def _setup_otlp_pipeline(
        self, resource: Resource, endpoint: str | None
    ) -> None:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        kwargs = {"endpoint": endpoint, "insecure": True} if endpoint else {}

        self._tracer_provider = TracerProvider(
            resource=resource, sampler=ParentBased(ALWAYS_ON)
        )
        self._tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(**kwargs))
        )
        trace.set_tracer_provider(self._tracer_provider)

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(**kwargs), export_interval_millis=15000
        )
        self._meter_provider = MeterProvider(
            resource=resource, metric_readers=[reader]
        )
        metrics.set_meter_provider(self._meter_provider)

        self._logger_provider = LoggerProvider(resource=resource)
        self._logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(**kwargs))
        )
        from opentelemetry._logs import set_logger_provider

        set_logger_provider(self._logger_provider)

    def force_flush(self) -> None:
        """Flushes all active telemetry buffers."""
        with self._lock:
            for name, provider in [
                ("tracer", self._tracer_provider),
                ("meter", self._meter_provider),
                ("logger", self._logger_provider),
            ]:
                if provider and hasattr(provider, "force_flush"):
                    try:
                        provider.force_flush()
                    except Exception:
                        logger.exception(
                            "telemetry_provider_flush_exception", provider=name
                        )

    def shutdown(self) -> None:
        """Shuts down all telemetry providers."""
        with self._lock:
            if self._tracer_provider:
                try:
                    self._tracer_provider.shutdown()
                except Exception:
                    logger.exception("tracer_provider_shutdown_failed")
            if self._meter_provider:
                try:
                    self._meter_provider.shutdown()
                except Exception:
                    logger.exception("meter_provider_shutdown_failed")
            if self._logger_provider:
                try:
                    self._logger_provider.shutdown()
                except Exception:
                    logger.exception("logger_provider_shutdown_failed")

            self._tracer_provider = None
            self._meter_provider = None
            self._logger_provider = None
            self._is_initialized = False
            self._is_shutdown = True
            self._state_version += 1


_MANAGER = TelemetryManager()
_REGISTRY = InstrumentRegistry()


def configure_telemetry(
    service_name: str,
    environment: str = "production",
    version: str = "1.0.0",
    otlp_endpoint: str | None = None,
) -> tuple[TracerProvider | None, MeterProvider | None, Any | None]:
    """Configures the global telemetry environment."""
    return _MANAGER.configure(service_name, environment, version, otlp_endpoint)


def flush_telemetry() -> None:
    """Forces flushing of remaining telemetry data."""
    _MANAGER.force_flush()


def shutdown_telemetry() -> None:
    """Shuts down the telemetry pipeline."""
    _MANAGER.shutdown()
    _REGISTRY.clear()


class TraceContext:
    """Utility for handling W3C distributed trace contexts."""

    __slots__ = ()

    @staticmethod
    def extract_from_w3c(traceparent: str | None) -> context.Context:
        """Extracts OTel context from a W3C traceparent header."""
        if not traceparent:
            return context.get_current()
        return _PROPAGATOR.extract(carrier={"traceparent": traceparent})


class _UdfTraceContext:
    """Context manager for unifying logging and tracing execution."""

    __slots__ = (
        "target_name",
        "static_labels",
        "success_labels",
        "counter",
        "histogram",
        "links",
        "start_time",
        "_span_manager",
        "_log_manager",
    )

    def __init__(
        self,
        target_name: str,
        static_labels: dict[str, str],
        success_labels: dict[str, str],
        counter: Any,
        histogram: Any,
        links: list[Link],
    ):
        self.target_name = target_name
        self.static_labels = static_labels
        self.success_labels = success_labels
        self.counter = counter
        self.histogram = histogram
        self.links = links
        self.start_time = 0.0
        self._span_manager: Any = None
        self._log_manager: Any = None

    def __enter__(self) -> None:
        self.counter.add(1, self.static_labels)
        self.start_time = time.perf_counter()
        tracer = trace.get_tracer("telemetry.engine")
        self._span_manager = tracer.start_as_current_span(
            self.target_name, links=self.links
        )
        self._span_manager.__enter__()
        self._log_manager = structlog.contextvars.bound_contextvars(
            span_name=self.target_name
        )
        self._log_manager.__enter__()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool | None:
        duration = time.perf_counter() - self.start_time
        span = trace.get_current_span()

        if exc_type is None:
            span.set_status(Status(StatusCode.OK))
            self.histogram.record(duration, self.success_labels)
        else:
            span.record_exception(exc_val)
            span.set_status(Status(StatusCode.ERROR, description=str(exc_val)))

        if self._log_manager:
            self._log_manager.__exit__(exc_type, exc_val, exc_tb)
        if self._span_manager:
            self._span_manager.__exit__(exc_type, exc_val, exc_tb)
        return False


def _build_span_links(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> list[Link]:
    trace_parents = kwargs.get("trace_parents") or kwargs.get("trace_ids")

    if not trace_parents and args:
        for arg in args:
            if isinstance(arg, list):
                trace_parents = arg
                break

    if not isinstance(trace_parents, list):
        return []

    unique_parents = list(
        dict.fromkeys(p for p in trace_parents if isinstance(p, str) and p)
    )
    links: list[Link] = []

    for tp in unique_parents:
        ctx = _PROPAGATOR.extract(carrier={"traceparent": tp})
        span_ctx = trace.get_current_span(context=ctx).get_span_context()
        if span_ctx.is_valid:
            links.append(Link(context=span_ctx))
    return links


@overload
def instrument(span_name: str) -> Callable[[F], F]: ...


@overload
def instrument(span_name: None = None) -> Callable[[F], F]: ...


def instrument(span_name: str | None = None) -> Callable[[F], F]:
    """Decorator to instrument function execution with traces and metrics."""

    def decorator(func: F) -> F:
        target_name = span_name or func.__name__
        static_labels = {"udf.name": target_name}
        success_labels = {"udf.name": target_name, "status": "success"}

        local_version = -1
        cached_counter: Any = None
        cached_histogram: Any = None
        cached_fail_counter: Any = None

        def _resolve_instruments() -> tuple[Any, Any]:
            nonlocal local_version, cached_counter, cached_histogram
            current_version = _MANAGER.state_version
            if cached_counter is None or local_version != current_version:
                cached_counter = _REGISTRY.get_instrument(
                    "counter",
                    "pipeline_udf_executions_total",
                    "Total executions",
                )
                cached_histogram = _REGISTRY.get_instrument(
                    "histogram",
                    "pipeline_udf_duration_seconds",
                    "Execution latency",
                    unit="s",
                )
                local_version = current_version
            return cached_counter, cached_histogram

        def _resolve_failure_counter() -> Any:
            nonlocal local_version, cached_fail_counter
            current_version = _MANAGER.state_version
            if cached_fail_counter is None or local_version != current_version:
                cached_fail_counter = _REGISTRY.get_instrument(
                    "counter",
                    "pipeline_udf_failures_total",
                    "Total execution failures",
                )
            return cached_fail_counter

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            counter, histogram = _resolve_instruments()
            links = _build_span_links(args, kwargs)

            with _UdfTraceContext(
                target_name,
                static_labels,
                success_labels,
                counter,
                histogram,
                links,
            ):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    _resolve_failure_counter().add(
                        1,
                        {"udf.name": target_name, "error": type(exc).__name__},
                    )
                    logger.exception("async_udf_execution_failed")
                    raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            counter, histogram = _resolve_instruments()
            links = _build_span_links(args, kwargs)

            with _UdfTraceContext(
                target_name,
                static_labels,
                success_labels,
                counter,
                histogram,
                links,
            ):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    _resolve_failure_counter().add(
                        1,
                        {"udf.name": target_name, "error": type(exc).__name__},
                    )
                    logger.exception("sync_udf_execution_failed")
                    raise

        return (
            async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper
        )  # type: ignore

    return decorator
