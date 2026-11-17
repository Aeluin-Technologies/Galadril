"""Unit tests for FastStream-independent ingress and command handlers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4, uuid5

import pytest
from galadril_ontology import GolDenied, GolLease, GolRequest, GolRuntimeManager
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
from galadril_vision.common.config import PipelineStepConfig, VisionConfig
from galadril_vision.common.schemas import CanonicalRecord, ObservationLineage
from galadril_vision.pipeline.ledger import MemoryExecutionLedger
from galadril_vision.streaming.handlers import (
    AvroEnvelope,
    CommandHandler,
    IngressHandler,
)
from galadril_vision.streaming.security import GatewayPublicationGate
from galadril_vision.streaming.topics import TopicLayout
from galadril_vision.telemetry.metrics import PipelineMetrics


@pytest.fixture
def anyio_backend() -> str:
    """Runs async handler contracts on the production asyncio backend."""
    return "asyncio"


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
            admission=command.admission,
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
        tenant_id="tenant-1",
        pipeline_id="vision",
        revision_id="revision-1",
        payload={
            "id": "record-1",
            "timestamp": 1_700_000_000_000,
            "ingested_at": 1_700_000_000_100,
            "storage_path": "s3://raw/tenant-1/source/object",
            "source": "camera-1",
            "authz": {
                "tenant_id": "tenant-1",
                "gol_admission": "opaque-server-handle",
                "source_principal": "service:intake",
                "execution_identity": "service:intake",
                "authentication_provenance": "https://issuer.example",
                "delegation_id": "delegation-test",
                "requested_permission": "materialize",
                "requested_resource": "raw:tenant-1/source/object",
                "tuples": [
                    {
                        "resource": "raw:tenant-1/source/object",
                        "relation": "parent",
                        "subject": "tenant:tenant-1",
                    }
                ],
            },
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
        publication_gate=_PublicationGate(),
    )
    correlation_id = UUID("8a445b78-e6d5-57c0-98c4-cf2851ad25bc")
    record = CanonicalRecord(
        record_id="obs-1",
        admission="server-admission",
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

    assert commands[0].admission == "server-admission"
    assert "admission" not in commands[0].payload["record"]
    assert "admission" not in commands[0].attributes
    assert commands[0].correlation_id == uuid5(
        correlation_id, "image_source:obs-1"
    )
    assert commands[0].attributes["source_correlation_id"] == str(
        correlation_id
    )
    assert commands[0].attributes["ingestion_id"] == "ing-1"
    assert commands[0].attributes["input_type"] == "image"
    assert commands[0].attributes["trace_id"] == (
        "4bf92f3577b34da6a3ce929d0e0e4736"
    )


def test_ingress_rows_from_one_object_have_distinct_command_ids() -> None:
    """One CSV object may yield many independently idempotent observations."""
    handler = IngressHandler(
        pipeline="vision",
        routes=PipelineRouteTable(_pipeline()),
        publisher=_Publisher(),
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
        publication_gate=_PublicationGate(),
    )
    source_id = "8a445b78-e6d5-57c0-98c4-cf2851ad25bc"

    def record(observation_id: str) -> CanonicalRecord:
        return CanonicalRecord(
            record_id=observation_id,
            tenant_id="tenant-1",
            lineage=ObservationLineage(
                ingestion_id="ing-1",
                trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
                span_id="00f067aa0ba902b7",
                source_event_id="s3:ObjectCreated:Put",
                correlation_id=source_id,
                schema_version="3.0.0",
                idempotency_key=observation_id,
            ),
        )

    first = handler._commands("image_source", "raw", record("row-0"))[0]
    second = handler._commands("image_source", "raw", record("row-1"))[0]
    replay = handler._commands("image_source", "raw", record("row-0"))[0]

    assert first.correlation_id != second.correlation_id
    assert first.event_id != second.event_id
    assert first.event_id == replay.event_id
    assert first.attributes["source_correlation_id"] == source_id
    assert second.attributes["source_correlation_id"] == source_id


@pytest.mark.anyio
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
        publication_gate=_PublicationGate(),
    )

    first = await handler.handle(_envelope())
    second = await handler.handle(_envelope())

    assert first[0].event_id == second[0].event_id
    record = first[0].payload["record"]
    assert isinstance(record, dict)
    assert record["tenant_id"] == "tenant-1"
    assert publisher.items[0].topic == TopicLayout().commands_gpu
    assert publisher.items[1].topic == TopicLayout().lineage


def test_pipeline_identity_separates_shared_ingress_command_ids() -> None:
    """The same tenant record can fan out without command ID collisions."""
    record = CanonicalRecord(
        record_id="record-1",
        tenant_id="tenant-1",
        source="image_source",
        input_type="image",
    )
    first = IngressHandler(
        pipeline="tenant-1/first/revision-a",
        tenant_id="tenant-1",
        routes=PipelineRouteTable(_pipeline()),
        publisher=_Publisher(),
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
        publication_gate=_PublicationGate(),
    )._commands("image_source", "raw", record)
    second = IngressHandler(
        pipeline="tenant-1/second/revision-b",
        tenant_id="tenant-1",
        routes=PipelineRouteTable(_pipeline()),
        publisher=_Publisher(),
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
        publication_gate=_PublicationGate(),
    )._commands("image_source", "raw", record)

    assert first[0].event_id != second[0].event_id


@pytest.mark.anyio
async def test_invalid_ingress_records_no_raw_quarantine_payload() -> None:
    """Malformed inputs have no authenticated release route for error metadata."""
    publisher = _Publisher()
    handler = IngressHandler(
        pipeline="vision",
        routes=PipelineRouteTable(_pipeline()),
        publisher=publisher,
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
        publication_gate=_PublicationGate(),
    )
    invalid = _envelope().model_copy(
        update={"payload": {"id": "record-1", "source": "camera"}}
    )

    commands = await handler.handle(invalid)

    assert commands == ()
    assert publisher.items == []


@pytest.mark.anyio
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
        publication_gate=_PublicationGate(),
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


@pytest.mark.anyio
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
        publication_gate=_PublicationGate(),
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


@pytest.mark.anyio
async def test_routing_decision_survives_replay_without_reclassifying() -> None:
    config = _pipeline()
    config.pipeline[:] = [
        PipelineStep.model_validate(
            {
                "step": "classify",
                "type": "route",
                "input_from": ["image_source"],
                "routing": "slm",
                "branches": {"legal": ["legal"], "other": ["other"]},
            }
        ),
        PipelineStep(step="legal", type="sink", input_from=["classify"]),
        PipelineStep(step="other", type="sink", input_from=["classify"]),
    ]

    class Classifier(_Dispatcher):
        async def dispatch(self, command: PipelineCommand) -> StepResult:
            result = await super().dispatch(command)
            return result.model_copy(
                update={
                    "output": {
                        "branch": "legal" if self.calls == 1 else "other",
                        "routing_model": "api-backup"
                        if self.calls == 1
                        else "local",
                        "routing_fallback": self.calls == 1,
                    }
                }
            )

    dispatcher = Classifier()
    publisher = _Publisher()
    topics = TopicLayout()
    handler = CommandHandler(
        routes=PipelineRouteTable(config),
        publisher=publisher,
        dispatcher=dispatcher,
        ledger=MemoryExecutionLedger(),
        topics=topics,
        metrics=PipelineMetrics(),
        publication_gate=_PublicationGate(),
    )
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="classify",
        step_type="route",
        resource_class="cpu",
    )
    await handler.handle(command)
    await handler.handle(command)
    assert dispatcher.calls == 1
    successors = [
        item.message
        for item in publisher.items
        if item.topic == topics.commands_for(ResourceClass.CPU)
    ]
    assert [item["step"] for item in successors] == ["legal", "legal"]
    assert successors[0]["event_id"] == successors[1]["event_id"]
    assert all(
        item["payload"]["routing_model"] == "api-backup"
        and item["payload"]["routing_fallback"] is True
        for item in successors
    )


class _PublicationGate:
    def __init__(self) -> None:
        self.allowed = True
        self.calls = 0

    async def authorize_state(self, command: PipelineCommand) -> None:
        return None

    @asynccontextmanager
    async def protect(self, command: PipelineCommand) -> AsyncIterator[None]:
        self.calls += 1
        if not self.allowed:
            raise GolDenied("GOL publication unavailable")
        yield


@pytest.mark.anyio
async def test_ingress_without_authority_publishes_no_payload() -> None:
    publisher = _Publisher()
    handler = IngressHandler(
        pipeline="vision",
        routes=PipelineRouteTable(_pipeline()),
        publisher=publisher,
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
    )
    assert await handler.handle(_envelope()) == ()
    assert publisher.items == []


@pytest.mark.anyio
async def test_malformed_ingress_cannot_disclose_its_raw_error_or_payload() -> (
    None
):
    publisher = _Publisher()
    handler = IngressHandler(
        pipeline="vision",
        routes=PipelineRouteTable(_pipeline()),
        publisher=publisher,
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
    )
    envelope = _envelope().model_copy(
        update={"payload": {"secret_field_name": "secret_value"}}
    )
    assert await handler.handle(envelope) == ()
    assert publisher.items == []


@pytest.mark.anyio
async def test_completed_replay_rechecks_transfer_without_reexecuting_effects() -> (
    None
):
    publisher = _Publisher()
    gate = _PublicationGate()
    dispatcher = _Dispatcher()
    handler = CommandHandler(
        routes=PipelineRouteTable(_pipeline()),
        publisher=publisher,
        dispatcher=dispatcher,
        ledger=MemoryExecutionLedger(),
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
        publication_gate=gate,
    )
    command = PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="infer",
        step_type=StepType.INFERENCE,
        resource_class=ResourceClass.GPU,
        admission="opaque-server-handle",
    )
    await handler.handle(command)
    first_count = len(publisher.items)
    gate.allowed = False
    assert await handler.handle(command) is None
    assert len(publisher.items) == first_count
    assert dispatcher.calls == 1
    assert gate.calls > first_count


@pytest.mark.anyio
async def test_concrete_gate_preserves_scope_and_fresh_release_checks() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.opens: list[tuple[GolRequest, str]] = []
            self.transfers = 0
            self.allowed = True

        async def open(self, request: GolRequest, admission: str) -> GolLease:
            self.opens.append((request, admission))
            return GolLease.model_validate(
                {
                    "tenantId": "tenant-1",
                    "ontologyId": "communication.mail",
                    "ontologyVersion": "1.0.0",
                    "sourceRevision": "source-one",
                    "generationId": "generation-one",
                    "contextId": "same-context",
                    "symbols": [
                        {
                            "key": "communication.mail::Message",
                            "name": "Message",
                            "kind": "entity",
                            "version": "1.0.0",
                        }
                    ],
                }
            )

        @asynccontextmanager
        async def protected(
            self,
            request: GolRequest,
            selected: GolLease,
            admission: str,
            *,
            effects: object,
        ) -> AsyncIterator[tuple[None]]:
            self.transfers += 1
            if not self.allowed:
                raise GolDenied("GOL publication unavailable")
            yield (None,)

    gateway = Gateway()
    config = VisionConfig.model_construct(
        name="tenant-1/mail/revision-one",
        runtime_tenant_id="tenant-1",
        runtime_pipeline_id="mail",
        runtime_revision_id="revision-one",
        pipeline=[
            PipelineStepConfig(
                step="sink",
                type="sink",
                input_from=["infer"],
                origin={
                    "pipeline_id": "origin-mail",
                    "revision_id": "origin-revision-1234567890",
                    "block_id": "origin-sink",
                },
                params={
                    "gol_transfer_symbol_key": "communication.mail::Message"
                },
            )
        ],
    )
    gate = GatewayPublicationGate(config, GolRuntimeManager(gateway))
    command = PipelineCommand(
        correlation_id=uuid4(),
        tenant_id="tenant-1",
        pipeline=config.name,
        step="sink",
        step_type="sink",
        resource_class="cpu",
        admission="opaque-server-handle",
        payload={"record": {"storage_path": "s3://raw/tenant-1/source/object"}},
    )
    await gate.authorize_state(command)
    async with gate.protect(command):
        pass
    gateway.allowed = False
    with pytest.raises(GolDenied):
        async with gate.protect(command):
            pass
    assert gateway.transfers == 2
    assert len(gateway.opens) == 3
    assert all(
        request.pipeline_id == "origin-mail"
        and request.pipeline_revision_id == "origin-revision-1234567890"
        and request.block_id == "origin-sink"
        and request.source_object_id == "raw:tenant-1/source/object"
        and admission == "opaque-server-handle"
        for request, admission in gateway.opens
    )
    for untrusted in (
        command.model_copy(update={"tenant_id": "tenant-2"}),
        command.model_copy(update={"admission": None}),
        command.model_copy(
            update={
                "payload": {
                    "record": {"storage_path": "s3://raw/tenant-2/secret"}
                }
            }
        ),
    ):
        with pytest.raises(GolDenied):
            async with gate.protect(untrusted):
                pass
    assert len(gateway.opens) == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))


@pytest.mark.anyio
async def test_kafka_io_remains_inside_the_live_permit() -> None:
    class Gate(_PublicationGate):
        held = False

        @asynccontextmanager
        async def protect(
            self, command: PipelineCommand
        ) -> AsyncIterator[None]:
            self.held = True
            try:
                yield
            finally:
                self.held = False

    gate = Gate()

    class Publisher(_Publisher):
        async def publish(
            self, message: object, topic: str, **kwargs: object
        ) -> object:
            assert gate.held, (
                "Kafka I/O escaped the host-retained effect boundary"
            )
            return await super().publish(message, topic, **kwargs)

    publisher = Publisher()
    handler = IngressHandler(
        pipeline="vision",
        routes=PipelineRouteTable(_pipeline()),
        publisher=publisher,
        topics=TopicLayout(),
        metrics=PipelineMetrics(),
        publication_gate=gate,
    )
    assert await handler.handle(_envelope())
    assert len(publisher.items) == 2
    assert not gate.held
