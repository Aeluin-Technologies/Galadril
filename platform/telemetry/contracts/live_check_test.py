"""Validates the OTLP emitted by the production Vision contract adapters."""

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
from uuid import uuid4

from galadril_pipeline.config import StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass, StepResult
from galadril_pipeline.telemetry_contract import ErrorCode, VisionPhase
from galadril_vision.actors.worker import PipelineActor
from galadril_vision.application.errors import CommandProcessingError
from galadril_vision.telemetry.metrics import PipelineMetrics
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from pydantic import JsonValue

_PRIVATE_VALUE = "private test receipt under no export"


class RejectingProcessor:
    async def process(self, _command: PipelineCommand) -> dict[str, JsonValue]:
        raise CommandProcessingError(
            _PRIVATE_VALUE,
            code=ErrorCode.ACCESS_DENIED,
            phase=VisionPhase.HANDLER,
        )


def emit(endpoint: str) -> str:
    """Exports a real actor error and scheduler metrics through OTLP gRPC."""
    resource = Resource({"service.name": "galadril-vision"})
    logger = LoggerProvider(resource=resource, shutdown_on_exit=False)
    logger.add_log_record_processor(
        SimpleLogRecordProcessor(
            OTLPLogExporter(endpoint=endpoint, insecure=True, timeout=3)
        )
    )
    tracer = TracerProvider(resource=resource, shutdown_on_exit=False)
    tracer.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, insecure=True, timeout=3)
        )
    )
    meter = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=True, timeout=3)
            )
        ],
        shutdown_on_exit=False,
    )
    set_logger_provider(logger)
    trace.set_tracer_provider(tracer)
    metrics.set_meter_provider(meter)
    try:
        command = PipelineCommand(
            correlation_id=uuid4(),
            pipeline=_PRIVATE_VALUE,
            entity_id=_PRIVATE_VALUE,
            step="infer",
            step_type=StepType.INFERENCE,
            resource_class=ResourceClass.GPU,
        )
        result = StepResult.model_validate_json(
            asyncio.run(
                PipelineActor(RejectingProcessor()).execute(
                    command.model_dump_json().encode(), {}
                )
            )
        )
        assert result.failure is not None
        assert result.failure.code is ErrorCode.ACCESS_DENIED
        instruments = PipelineMetrics()
        instruments.ray_task_started(ResourceClass.GPU)
        meter.force_flush()
        return str(result.failure.occurrence_id)
    finally:
        meter.shutdown()
        tracer.shutdown()
        logger.shutdown()


def wait_for_receiver(
    process: subprocess.Popen[bytes], startup: Path
) -> tuple[str, str]:
    """Discovers OS-assigned endpoints without fixed test ports."""
    deadline = time.monotonic() + 15
    while process.poll() is None and time.monotonic() < deadline:
        lines = startup.read_text().splitlines()
        grpc_endpoint = next(
            (
                line.rsplit(" ", 1)[-1]
                for line in lines
                if "OTLP gRPC listener started on " in line
            ),
            "",
        )
        admin_stop = next(
            (
                line.rsplit(" ", 1)[-1]
                for line in lines
                if "or POST to http://" in line and line.endswith("/stop")
            ),
            "",
        )
        if grpc_endpoint and admin_stop:
            return grpc_endpoint, admin_stop
        time.sleep(0.05)
    raise AssertionError(f"Weaver did not start:\n{startup.read_text()}")


def main(weaver: Path, manifest: Path, upstream: Path, config: Path) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        shutil.copytree(upstream.parent, root / "semconv")
        shutil.copytree(manifest.parent, root / "registry")
        startup = root / "weaver.log"
        with (
            startup.open("wb") as output,
            subprocess.Popen(
                [
                    str(weaver),
                    "registry",
                    "live-check",
                    "--v2",
                    "--config",
                    str(config),
                    "-r",
                    "registry",
                    "--otlp-grpc-address",
                    "127.0.0.1",
                    "--otlp-grpc-port",
                    "0",
                    "--admin-port",
                    "0",
                    "--inactivity-timeout",
                    "20",
                    "--fail-on",
                    "violation",
                    "--format",
                    "jsonl",
                ],
                cwd=root,
                stdout=output,
                stderr=subprocess.STDOUT,
            ) as process,
        ):
            try:
                endpoint, stop = wait_for_receiver(process, startup)
                occurrence = emit(endpoint)
                with urlopen(Request(stop, method="POST"), timeout=3):
                    pass
                exit_code = process.wait(timeout=15)
                report = startup.read_text()
                assert exit_code == 0, report
                samples = [
                    json.loads(line)
                    for line in report.splitlines()
                    if line.startswith("{")
                ]
                (span,) = (
                    sample["span"] for sample in samples if "span" in sample
                )
                (log,) = (
                    sample["log"] for sample in samples if "log" in sample
                )
                resources = [
                    sample["resource"]
                    for sample in samples
                    if "resource" in sample
                ]
                assert resources
                assert all(
                    any(
                        attr["name"] == "service.name"
                        and attr["value"] == "galadril-vision"
                        for attr in resource["attributes"]
                    )
                    for resource in resources
                )
                assert span["name"] == "galadril.vision.step.execute"
                assert (
                    span["live_check_result"]["match_info"]["signal"]
                    == span["name"]
                )
                assert span["status"]["code"] == "error"
                assert span["trace_id"] == log["trace_id"]
                assert span["span_id"] == log["span_id"]
                assert log["event_name"] == "galadril.vision.step.failed"
                assert occurrence in report
                assert any(
                    sample.get("metric", {}).get("name")
                    == "galadril.vision.task.active"
                    for sample in samples
                )
                assert _PRIVATE_VALUE not in report
            finally:
                if process.poll() is None:
                    process.kill()


if __name__ == "__main__":
    main(*(Path(argument).absolute() for argument in sys.argv[1:]))
