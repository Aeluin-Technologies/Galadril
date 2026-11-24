"""Kafka release reuses the original server calculation and its dependency history."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from galadril_ontology import (
    GolBlockContract,
    GolDenied,
    GolEffect,
    GolHandoff,
    GolRequest,
    GolRuntimeManager,
    source_object_id,
)
from galadril_pipeline.events import PipelineCommand

from galadril_vision.common.config import VisionConfig


class PublicationGate(Protocol):
    """Every broker write requires a live release decision, including cache hits."""

    async def authorize_state(self, command: PipelineCommand) -> None: ...
    async def resume(self, command: PipelineCommand) -> str: ...
    def protect(
        self,
        command: PipelineCommand,
        *,
        handoff: PipelineCommand | None = None,
    ) -> AbstractAsyncContextManager[str | None]: ...


@dataclass(frozen=True, slots=True)
class _Binding:
    step_type: str
    target: str | None


class GatewayPublicationGate:
    """Gateway retains flow and all historical authorization dependencies."""

    __slots__ = (
        "_runtime",
        "_execution",
        "_tenant",
        "_pipeline",
        "_revision",
        "_bindings",
    )

    def __init__(
        self, config: VisionConfig, runtime: GolRuntimeManager
    ) -> None:
        self._runtime = runtime
        self._execution = config.name
        self._tenant = config.runtime_tenant_id
        self._pipeline = config.ontology_pipeline_id
        self._revision = config.runtime_revision_id
        self._bindings: Mapping[str, _Binding] = MappingProxyType(
            {
                step.step: _Binding(
                    step.type,
                    value
                    if isinstance(
                        (value := step.params.get("gol_transfer_symbol_key")),
                        str,
                    )
                    else None,
                )
                for step in config.pipeline
            }
        )

    def _request(self, command: PipelineCommand) -> tuple[GolRequest, str]:
        binding = self._bindings.get(command.step)
        record = command.payload.get("record")
        if (
            binding is None
            or not binding.target
            or not self._revision
            or not self._tenant
            or command.tenant_id != self._tenant
            or command.pipeline != self._execution
            or command.step_type.value != binding.step_type
            or not isinstance(record, dict)
        ):
            raise GolDenied("GOL publication unavailable")
        try:
            source = source_object_id(
                record.get("storage_path"), command.tenant_id
            )
            request = GolRequest(
                tenant_id=command.tenant_id,
                pipeline_id=self._pipeline,
                pipeline_revision_id=self._revision,
                block_id=command.step,
                source_object_id=source,
                metadata_target=binding.target,
            )
        except ValueError:
            raise GolDenied("GOL publication unavailable") from None
        return request, binding.target

    async def authorize_state(self, command: PipelineCommand) -> None:
        """Restores the same admitted calculation before reading the execution ledger."""
        request, target = self._request(command)
        async with self._runtime.bind(
            request,
            GolBlockContract(required_symbol_keys=(target,)),
            admission=command.admission,
        ):
            pass

    async def resume(self, command: PipelineCommand) -> str:
        """Resumes an immutable command without carrying live permissions."""
        if command.lineage is None:
            raise GolDenied("GOL lineage unavailable")
        request, _ = self._request(command)
        return await self._runtime.resume(
            request, command.lineage, command.event_id
        )

    @asynccontextmanager
    async def protect(
        self,
        command: PipelineCommand,
        *,
        handoff: PipelineCommand | None = None,
    ) -> AsyncIterator[str | None]:
        """A newly filtered projection cannot erase previously observed dependencies."""
        request, target = self._request(command)
        continuation = None
        if handoff is not None:
            recipient, _ = self._request(handoff)
            if request.source_object_id != recipient.source_object_id:
                raise GolDenied("GOL command handoff changed source")
            continuation = GolHandoff(
                blockId=recipient.block_id, commandId=handoff.event_id
            )
        async with self._runtime.bind(
            request,
            GolBlockContract(required_symbol_keys=(target,)),
            admission=command.admission,
        ):
            async with self._runtime.protected(
                (
                    GolEffect(
                        operation="transfer",
                        target=target,
                        destination="kafka",
                        purpose="pipeline_dispatch",
                        handoff=continuation,
                    ),
                )
            ) as (receipt,):
                if handoff is not None and not receipt:
                    raise GolDenied("GOL command lineage unavailable")
                yield receipt
