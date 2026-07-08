"""Unit and integration tests for the telemetry and logging subsystem."""

import asyncio
import logging
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from galadril_vision.telemetry.logging import (
    OTLPContextProcessor,
    configure_logging,
)
from galadril_vision.telemetry.tracing import (
    _MANAGER,
    _REGISTRY,
    InstrumentRegistry,
    TelemetryManager,
    instrument,
)
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture(autouse=True)
def reset_telemetry_globals() -> Generator[None, None, None]:
    """Resets global telemetry manager and registry states before and after each test."""
    _REGISTRY.clear()

    yield

    _MANAGER.shutdown()
    _REGISTRY.clear()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def memory_telemetry() -> Generator[
    tuple[InMemorySpanExporter, InMemoryMetricReader], None, None
]:
    """Sets up a pure in-memory OpenTelemetry environment for strict assertions."""
    tracer_provider = TracerProvider()
    span_exporter = InMemorySpanExporter()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    yield span_exporter, metric_reader


def test_otlp_context_processor_injects_valid_trace_context(
    memory_telemetry: Any,
) -> None:
    """Verifies that OTLPContextProcessor correctly extracts and injects active trace/span IDs."""
    processor = OTLPContextProcessor()
    event_dict: structlog.types.EventDict = {"event": "test_log"}

    # No active span.
    processed = processor(None, "info", event_dict.copy())
    assert "trace_id" not in processed
    assert "span_id" not in processed

    # Active and valid span.
    tracer = trace.get_tracer("test.tracer")
    with tracer.start_as_current_span("active_span") as span:
        processed_with_span = processor(None, "info", event_dict.copy())
        ctx = span.get_span_context()

        assert processed_with_span["trace_id"] == f"{ctx.trace_id:032x}"
        assert processed_with_span["span_id"] == f"{ctx.span_id:016x}"


def test_configure_logging_idempotency() -> None:
    """Validates that configure_logging initializes handlers and formatters without breaking."""
    root_logger = logging.getLogger()
    initial_handler_count = len(root_logger.handlers)

    configure_logging(default_level="DEBUG", enable_json_format=True)
    assert len(root_logger.handlers) == max(initial_handler_count, 1)

    # Idempotence check.
    configure_logging(default_level="INFO", enable_json_format=True)

    mock_provider = MagicMock()
    configure_logging(default_level="INFO", otlp_logger_provider=mock_provider)


def test_instrument_registry_caching_and_validation(
    memory_telemetry: Any,
) -> None:
    """Validates thread-safe caching, reuse of instruments, and rejection of invalid types."""
    registry = InstrumentRegistry()

    counter1 = registry.get_instrument(
        "counter", "test_counter", "A test counter"
    )
    assert counter1 is not None

    counter2 = registry.get_instrument(
        "counter", "test_counter", "A test counter"
    )
    assert counter1 is counter2

    histogram = registry.get_instrument(
        "histogram", "test_histo", "A test histogram", unit="s"
    )
    assert histogram is not None

    with pytest.raises(ValueError, match="Unsupported instrument type"):
        registry.get_instrument("invalid_type", "bad_metric", "Should fail")


def test_telemetry_manager_lifecycle() -> None:
    """Tests the configuration, state tracking, and clean shutdown of the TelemetryManager."""
    manager = TelemetryManager()
    assert manager.state_version == 0

    t_prov, m_prov, l_prov = manager.configure(
        service_name="test-service",
        environment="test",
        version="0.0.1",
        otlp_endpoint="console",
    )
    assert manager.state_version == 1
    assert t_prov is not None
    assert m_prov is not None
    assert l_prov is not None

    t_prov2, _, _ = manager.configure("test-service")
    assert manager.state_version == 1
    assert t_prov2 is t_prov

    manager.force_flush()

    manager.shutdown()
    assert manager.state_version == 2

    with pytest.raises(RuntimeError, match="Telemetry manager is shut down"):
        manager.configure("test-service")


def test_sync_instrument_decorator_success(
    memory_telemetry: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    """Ensures synchronous @instrument produces spans and increments success metrics."""
    span_exporter, metric_reader = memory_telemetry

    @instrument(span_name="custom_sync_span")
    def compute_sync(x: int, y: int) -> int:
        return x + y

    result = compute_sync(10, 32)
    assert result == 42

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "custom_sync_span"
    assert spans[0].status.is_ok

    metrics_data = metric_reader.get_metrics_data()
    assert metrics_data is not None

    resource_metrics = metrics_data.resource_metrics
    assert len(resource_metrics) > 0

    metric_names = [
        metric.name
        for rm in resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    ]
    assert "pipeline_udf_executions_total" in metric_names
    assert "pipeline_udf_duration_seconds" in metric_names


def test_sync_instrument_decorator_failure(
    memory_telemetry: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    """Ensures synchronous @instrument records exceptions inside spans and routes to failure metrics."""
    span_exporter, metric_reader = memory_telemetry

    @instrument()
    def failing_sync() -> None:
        raise ValueError("Simulated computation crash")

    with pytest.raises(ValueError, match="Simulated computation crash"):
        failing_sync()

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "failing_sync"
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert len(spans[0].events) == 1
    assert spans[0].events[0].name == "exception"

    metrics_data = metric_reader.get_metrics_data()
    assert metrics_data is not None

    metric_names = [
        metric.name
        for rm in metrics_data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    ]
    assert "pipeline_udf_failures_total" in metric_names


@pytest.mark.asyncio
async def test_async_instrument_decorator_success(
    memory_telemetry: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    """Ensures asynchronous @instrument creates spans and updates metrics properly."""
    span_exporter, metric_reader = memory_telemetry

    @instrument(span_name="custom_async_span")
    async def compute_async() -> str:
        await asyncio.sleep(0.001)
        return "done"

    result = await compute_async()
    assert result == "done"

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "custom_async_span"
    assert spans[0].status.is_ok


@pytest.mark.asyncio
async def test_async_instrument_decorator_failure(
    memory_telemetry: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    """Ensures asynchronous @instrument logs and increments error counters on failure."""
    span_exporter, metric_reader = memory_telemetry

    @instrument()
    async def failing_async() -> None:
        await asyncio.sleep(0.001)
        raise RuntimeError("Async node fault")

    with pytest.raises(RuntimeError, match="Async node fault"):
        await failing_async()

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "failing_async"
    assert spans[0].status.status_code == trace.StatusCode.ERROR


def test_decorator_span_linking_from_context(
    memory_telemetry: tuple[InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    """Validates that trace_parents passed in kwargs are extracted and registered as standard OTel Span Links."""
    span_exporter, _ = memory_telemetry

    fake_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    fake_span_id = "00f067aa0ba902b7"
    valid_w3c_traceparent = f"00-{fake_trace_id}-{fake_span_id}-01"

    @instrument()
    def process_with_links(**kwargs: Any) -> None:
        pass

    process_with_links(trace_parents=[valid_w3c_traceparent])

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1

    assert len(spans[0].links) == 1
    linked_span_context = spans[0].links[0].context
    assert f"{linked_span_context.trace_id:032x}" == fake_trace_id
    assert f"{linked_span_context.span_id:016x}" == fake_span_id
