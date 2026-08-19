"""Unit tests for FastStream-independent ingress and command handlers."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from galadril_pipeline.config import (
    PipelineConfig,
    PipelineStep,
    RetryPolicy,
    Source,
    StepParams,
    StepType,
)
from galadril_pipeline.events import (
    EventStatus,
    PipelineCommand,
    ResourceClass,
    StepResult,
)
from galadril_pipeline.routing import PipelineRouteTable
from galadril_vision.common.schemas import CanonicalRecord, ObservationLineage
from galadril_vision.pipeline.ledger import MemoryExecutionLedger
from galadril_vision.streaming.handlers import (
    AvroEnvelope,
    CommandHandler,
    IngressHandler,
)
from galadril_vision.streaming.topics import TopicLayout
from galadril_vision.telemetry.metrics import PipelineMetrics


@dataclass(frozen=True, slots=True)
class _Published:
    message: object
    topic: str
    key: bytes | str | None


class _Publisher:
    """Captures confirmed broker writes in publication order."""

    def __init__(self) -> None:
        self.items: list[_Published] = []

    async def publish(
        self,
        message: object,
        topic: str,
        *,
        key: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        correlation_id: str | None = None,
        no_confirm: bool = False,
    ) -> object:
        del headers, correlation_id
        assert no_confirm is False
        item = _Published(message, topic, key)
        self.items.append(item)
        return item


class _Dispatcher:
    """Returns deterministic actor results and counts real executions."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def dispatch(self, command: PipelineCommand) -> StepResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return StepResult(
            correlation_id=command.correlation_id,
            causation_id=command.event_id,
            tenant_id=command.tenant_id,
            entity_id=command.entity_id,
            pipeline=command.pipeline,
            command_id=command.event_id,
            step=command.step,
            step_type=command.step_type,
            resource_class=command.resource_class,
            status=EventStatus.COMPLETED,
            duration_seconds=0.01,
            output={"record": {"record_id": "record-1"}, "data": {"ok": True}},
        )


def _pipeline(max_retries: int = 0) -> PipelineConfig:
    """Creates a two-step event route with configurable bounded retries."""
    return PipelineConfig(
        name="vision",
        sources=[
            Source(
                id="image_source",
                topic="raw",
                match_pattern=".*",
                schema_path="image.avsc",
            )
        ],
        pipeline=[
            PipelineStep(
                step="infer",
                type=StepType.INFERENCE,
                model="models.Face",
                input_from=["image_source"],
                params=StepParams(
                    retry_policy=RetryPolicy(max_retries=max_retries)
                ),
            ),
            PipelineStep(
                step="sink",
                type=StepType.SINK,
                input_from=["infer"],
            ),
        ],
    )


def _envelope() -> AvroEnvelope:
    """Builds a valid deserialized source message."""
    return AvroEnvelope(
        source_id="image_source",
        topic="raw",
        payload={
            "id": "record-1",
            "timestamp": 1_700_000_000_000,
            "ingested_at": 1_700_000_000_100,
            "storage_path": "s3://raw/image.jpg",
            "source": "camera-1",
            "authz": {"tenant_id": "tenant-1"},
            "mime_type": "image/jpeg",
        },
    )


def test_ingress_commands_preserve_intake_lineage_context() -> None:
    """Ensures LI-ESKG evidence provenance survives command construction."""
    handler = IngressHandler(
        pipeline="vision",
        routes=PipelineRouteTable(_pipeline()),
        publisher=_Publisher(),
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
    )
    correlation_id = UUID("8a445b78-e6d5-57c0-98c4-cf2851ad25bc")
    record = CanonicalRecord(
        record_id="obs-1",
        tenant_id="tenant-1",
        source="camera-east",
        input_type="image",
        concurrent_group_id="capture-1",
        lineage=ObservationLineage(
            ingestion_id="ing-1",
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            span_id="00f067aa0ba902b7",
            source_event_id="s3:ObjectCreated:Put",
            correlation_id=str(correlation_id),
            schema_version="3.0.0",
            idempotency_key="obs-1",
        ),
    )

    commands = handler._commands("image_source", "vision.silver", record)

    assert commands[0].correlation_id == correlation_id
    assert commands[0].attributes["ingestion_id"] == "ing-1"
    assert commands[0].attributes["input_type"] == "image"
    assert commands[0].attributes["trace_id"] == (
        "4bf92f3577b34da6a3ce929d0e0e4736"
    )


@pytest.mark.asyncio
async def test_ingress_validates_and_publishes_deterministic_entry_command() -> (
    None
):
    """Ensures raw replays produce the same command identity and confirmed topics."""
    publisher = _Publisher()
    routes = PipelineRouteTable(_pipeline())
    handler = IngressHandler(
        pipeline="vision",
        routes=routes,
        publisher=publisher,
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
    )

    first = await handler.handle(_envelope())
    second = await handler.handle(_envelope())

    assert first[0].event_id == second[0].event_id
    assert first[0].payload["record"]["tenant_id"] == "tenant-1"
    assert publisher.items[0].topic == TopicLayout().commands_gpu
    assert publisher.items[1].topic == TopicLayout().lineage


@pytest.mark.asyncio
async def test_ingress_quarantines_schema_violation() -> None:
    """Acknowledgement callers can safely commit after confirmed invalid publication."""
    publisher = _Publisher()
    handler = IngressHandler(
        pipeline="vision",
        routes=PipelineRouteTable(_pipeline()),
        publisher=publisher,
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
    )
    invalid = _envelope().model_copy(
        update={"payload": {"id": "record-1", "source": "camera"}}
    )

    commands = await handler.handle(invalid)

    assert commands == ()
    assert [item.topic for item in publisher.items] == [TopicLayout().invalid]


@pytest.mark.asyncio
async def test_completed_redelivery_skips_actor_and_replays_successors() -> (
    None
):
    """Proves durable completion avoids duplicate GPU execution after a crash."""
    publisher = _Publisher()
    dispatcher = _Dispatcher()
    routes = PipelineRouteTable(_pipeline())
    ledger = MemoryExecutionLedger()
    handler = CommandHandler(
        routes=routes,
        publisher=publisher,
        dispatcher=dispatcher,
        ledger=ledger,
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
    )
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="infer",
        step_type=StepType.INFERENCE,
        resource_class=ResourceClass.GPU,
    )

    await handler.handle(command)
    first_downstream = next(
        item
        for item in publisher.items
        if item.topic == TopicLayout().commands_cpu
    )
    completed_lineage = next(
        item.message
        for item in publisher.items
        if item.topic == TopicLayout().lineage
        and isinstance(item.message, dict)
        and item.message["status"] == EventStatus.COMPLETED.value
    )
    result_event = next(
        item.message
        for item in publisher.items
        if item.topic == TopicLayout().results
    )
    await handler.handle(command)
    replayed_downstream = [
        item
        for item in publisher.items
        if item.topic == TopicLayout().commands_cpu
    ][-1]

    assert dispatcher.calls == 1
    assert first_downstream.key == replayed_downstream.key
    assert isinstance(first_downstream.message, dict)
    assert isinstance(result_event, dict)
    assert isinstance(completed_lineage, dict)
    assert first_downstream.message["causation_id"] == result_event["event_id"]
    assert completed_lineage["output_refs"] == [result_event["event_id"]]


@pytest.mark.asyncio
async def test_actor_failure_publishes_bounded_retry() -> None:
    """Increments attempts durably instead of creating an infinite poison loop."""
    publisher = _Publisher()
    handler = CommandHandler(
        routes=PipelineRouteTable(_pipeline(max_retries=1)),
        publisher=publisher,
        dispatcher=_Dispatcher(RuntimeError("temporary GPU failure")),
        ledger=MemoryExecutionLedger(),
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
    )
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="infer",
        step_type=StepType.INFERENCE,
        resource_class=ResourceClass.GPU,
    )

    result = await handler.handle(command)

    retry = next(
        item.message
        for item in publisher.items
        if item.topic == TopicLayout().commands_gpu
    )
    assert result is None
    assert isinstance(retry, dict)
    assert retry["attempt"] == 1
    assert not any(
        item.topic == TopicLayout().dead_letter for item in publisher.items
    )
