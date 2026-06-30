"""Unit tests for OpenTelemetry instrumentation, lifecycle management, and structured logging integration."""

import logging
from unittest.mock import ANY, MagicMock, patch
import pytest
import structlog

from galadril_vision.telemetry.logging import (
    TelemetryConsoleHandler,
    configure_logging,
)
from galadril_vision.telemetry.tracing import (
    _MANAGER,
    _REGISTRY,
    configure_telemetry,
    instrument,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def setup_and_teardown():
    """Fixture to reset the global telemetry and logging states before and after each test."""
    _MANAGER.shutdown()
    _REGISTRY.clear()
    yield
    _MANAGER.shutdown()
    _REGISTRY.clear()

    root_logger = logging.getLogger()
    root_logger.handlers = []


def test_configure_telemetry_console_mode():
    """Verify that setting the endpoint to console correctly instantiates local providers."""
    with (
        patch("opentelemetry.sdk.trace.TracerProvider") as mock_trace_provider,
        patch("opentelemetry.sdk.metrics.MeterProvider") as mock_meter_provider,
        patch("opentelemetry.sdk._logs.LoggerProvider") as mock_logger_provider,
    ):
        tp, mp, lp = configure_telemetry(
            service_name="test-vision",
            environment="development",
            otlp_endpoint="console",
        )

        mock_trace_provider.assert_called_once()
        mock_meter_provider.assert_called_once()
        mock_logger_provider.assert_called_once()
        assert tp is not None
        assert mp is not None
        assert lp is not None


def test_configure_logging_console_and_json():
    """Verify that configure_logging applies the structlog processor formatter and detects handlers."""
    configure_logging(default_level="DEBUG", enable_json_format=True)

    root_logger = logging.getLogger()
    assert root_logger.level == logging.DEBUG

    handlers = [
        h
        for h in root_logger.handlers
        if isinstance(h, TelemetryConsoleHandler)
    ]
    assert len(handlers) == 1

    formatter = handlers[0].formatter
    assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)


@patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter")
@patch(
    "opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter"
)
@patch("opentelemetry.exporter.otlp.proto.grpc._log_exporter.OTLPLogExporter")
def test_configure_telemetry_otlp_mode(
    mock_log_exporter, mock_metric_exporter, mock_span_exporter
):
    """Verify that OTLP mode forwards the correct endpoint and insecurity flags to the gRPC exporters."""
    endpoint = "http://localhost:4317"

    tp, mp, lp = configure_telemetry(
        service_name="test-vision-prod",
        environment="production",
        otlp_endpoint=endpoint,
    )

    mock_span_exporter.assert_called_once_with(endpoint=endpoint, insecure=True)
    mock_metric_exporter.assert_called_once_with(
        endpoint=endpoint, insecure=True
    )
    mock_log_exporter.assert_called_once_with(endpoint=endpoint, insecure=True)


def test_instrument_sync_success():
    """Verify the instrument decorator tracks success metrics on synchronous functions."""
    mock_counter = MagicMock()
    mock_histogram = MagicMock()

    _REGISTRY.get_instrument = MagicMock(
        side_effect=lambda kind, *args, **kwargs: (
            mock_counter if kind == "counter" else mock_histogram
        )
    )

    @instrument(span_name="sync_test_func")
    def my_sync_function(x, y):
        return x + y

    result = my_sync_function(2, 3)

    assert result == 5
    mock_counter.add.assert_called_once_with(1, {"udf.name": "sync_test_func"})
    mock_histogram.record.assert_called_once_with(
        ANY, {"udf.name": "sync_test_func", "status": "success"}
    )


async def test_instrument_async_failure():
    """Verify the instrument decorator correctly updates failure metrics on asynchronous rejections."""
    mock_counter = MagicMock()
    mock_histogram = MagicMock()
    mock_fail_counter = MagicMock()

    def side_effect(kind, name, *args, **kwargs):
        if name == "pipeline_udf_failures_total":
            return mock_fail_counter
        return mock_counter if kind == "counter" else mock_histogram

    _REGISTRY.get_instrument = MagicMock(side_effect=side_effect)

    @instrument()
    async def my_async_function():
        raise ValueError("Execution halted")

    with pytest.raises(ValueError, match="Execution halted"):
        await my_async_function()

    mock_counter.add.assert_called_once()
    mock_fail_counter.add.assert_called_once_with(
        1, {"udf.name": "my_async_function", "error": "ValueError"}
    )
    mock_histogram.record.assert_not_called()


def test_build_span_links_with_traceparent():
    """Verify that distributed tracing context context is extracted from kwargs and added as span links."""
    from opentelemetry.trace import Link

    valid_traceparent = (
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    )

    mock_counter = MagicMock()
    mock_histogram = MagicMock()
    _REGISTRY.get_instrument = MagicMock(
        side_effect=lambda kind, *args, **kwargs: (
            mock_counter if kind == "counter" else mock_histogram
        )
    )

    with patch(
        "opentelemetry.sdk.trace.Tracer.start_as_current_span"
    ) as mock_start_span:

        @instrument()
        def process_data(**kwargs):
            return True

        process_data(trace_parents=[valid_traceparent])

        mock_start_span.assert_called_once()
        kwargs_passed = mock_start_span.call_args[1]

        assert "links" in kwargs_passed
        assert len(kwargs_passed["links"]) == 1
        assert isinstance(kwargs_passed["links"][0], Link)
