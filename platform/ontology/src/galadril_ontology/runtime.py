"""Bind immutable projections while Gateway owns calculation provenance."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

import orjson
import structlog
from opentelemetry import trace
from pydantic import JsonValue

from .contracts import (
    GolBlockContract,
    GolEffect,
    GolLease,
    GolOperation,
    GolRequest,
)
from .errors import GolDenied
from .identity import normalize_tenant_id

logger = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)


class RuntimeGateway(Protocol):
    async def open(self, request: GolRequest, admission: str) -> GolLease: ...
    async def resume(
        self, request: GolRequest, lineage: str, command_id: UUID
    ) -> str: ...
    def protected(
        self,
        request: GolRequest,
        selected: GolLease,
        admission: str,
        *,
        effects: Sequence[GolEffect],
    ) -> AbstractAsyncContextManager[tuple[str | None, ...]]: ...


@dataclass(frozen=True, slots=True)
class BoundCalculation:
    manager: GolRuntimeManager
    request: GolRequest
    lease: GolLease
    contract: GolBlockContract
    admission: str = field(repr=False)


_ACTIVE: ContextVar[BoundCalculation | None] = ContextVar(
    "gol_admitted_calculation", default=None
)


def active_gol_lease() -> GolLease | None:
    bound = _ACTIVE.get()
    return bound.lease if bound is not None else None


@dataclass(slots=True)
class _HeldBatch:
    manager: GolRuntimeManager
    effects: tuple[bytes, ...]
    receipts: tuple[str | None, ...]
    owner_task: asyncio.Task[object] | None
    active: bool = True


_EFFECT_ACTIVE: ContextVar[_HeldBatch | None] = ContextVar(
    "gol_effect_active", default=None
)


def scoped_instance_id(local_id: str) -> str:
    """Scopes newly created values without renaming existing resolved entities."""
    bound = _ACTIVE.get()
    if bound is None or not local_id or len(local_id) > 512:
        raise GolDenied("GOL instance identity unavailable")
    return (
        "gol:"
        + hashlib.sha256(bound.request.source_object_id.encode()).hexdigest()
        + ":"
        + hashlib.sha256(local_id.encode()).hexdigest()
    )


def make_gol_state_effect(
    *,
    operation: GolOperation,
    tenant_id: str,
    destination: str,
    purpose: str,
    proposal: dict[str, JsonValue] | None = None,
    instance_id: str | None = None,
    receipt: str | None = None,
    derived: bool = False,
) -> GolEffect:
    """Selecting an explicit state contract does not acquire permission."""
    bound = _ACTIVE.get()
    if (
        bound is None
        or normalize_tenant_id(tenant_id) != bound.request.tenant_id
    ):
        raise GolDenied("Shared state requires GOL admission")
    target = (
        bound.contract.derived_symbol_key
        if derived
        else bound.contract.state_symbol_key
    )
    if not target:
        raise GolDenied("Shared state requires an explicit GOL contract")
    return GolEffect(
        operation=operation,
        target=target,
        destination=destination,
        purpose=purpose,
        proposal=proposal,
        instance_id=instance_id,
        receipt=receipt,
    )


@asynccontextmanager
async def protected_gol_effects(
    effects: Sequence[GolEffect],
    *,
    tenant_id: str | None = None,
) -> AsyncIterator[tuple[str | None, ...]]:
    """All effects in a transaction share one host-retained exclusion boundary."""
    bound = _ACTIVE.get()
    if bound is None or (
        tenant_id is not None
        and normalize_tenant_id(tenant_id) != bound.request.tenant_id
    ):
        raise GolDenied("GOL effect unavailable")
    async with bound.manager.protected(effects) as receipts:
        yield receipts


@asynccontextmanager
async def protected_gol_effect(
    *,
    operation: GolOperation,
    target: str,
    destination: str,
    purpose: str,
    proposal: dict[str, JsonValue] | None = None,
    instance_id: str | None = None,
    receipt: str | None = None,
    tenant_id: str | None = None,
) -> AsyncIterator[str | None]:
    """The caller must place its actual I/O inside this scope."""
    effect = GolEffect(
        operation=operation,
        target=target,
        destination=destination,
        purpose=purpose,
        proposal=proposal,
        instance_id=instance_id,
        receipt=receipt,
    )
    async with protected_gol_effects(
        (effect,), tenant_id=tenant_id
    ) as receipts:
        yield next(iter(receipts))


@asynccontextmanager
async def protected_gol_state_effect(
    *,
    operation: GolOperation,
    tenant_id: str,
    destination: str,
    purpose: str,
    proposal: dict[str, JsonValue] | None = None,
    instance_id: str | None = None,
    receipt: str | None = None,
    derived: bool = False,
) -> AsyncIterator[str | None]:
    effect = make_gol_state_effect(
        operation=operation,
        tenant_id=tenant_id,
        destination=destination,
        purpose=purpose,
        proposal=proposal,
        instance_id=instance_id,
        receipt=receipt,
        derived=derived,
    )
    async with protected_gol_effects(
        (effect,), tenant_id=tenant_id
    ) as receipts:
        yield next(iter(receipts))


class GolRuntimeManager:
    """Task-local scope is never proof of independent information provenance."""

    __slots__ = ("_gateway",)

    def __init__(self, gateway: RuntimeGateway) -> None:
        self._gateway = gateway

    async def resume(
        self, request: GolRequest, lineage: str, command_id: UUID
    ) -> str:
        """Keeps re-admission outside any open effect scope."""
        if _ACTIVE.get() is not None or not lineage:
            raise GolDenied("GOL admission unavailable")
        return await self._gateway.resume(request, lineage, command_id)

    @asynccontextmanager
    async def bind(
        self,
        request: GolRequest,
        contract: GolBlockContract | None = None,
        *,
        admission: str | None,
    ) -> AsyncIterator[GolLease]:
        """Scopes transport handles without declaring a fresh IFC computation."""
        if not admission or _EFFECT_ACTIVE.get() is not None:
            raise GolDenied("GOL runtime unavailable")
        with tracer.start_as_current_span(
            "gol.runtime.open",
            record_exception=False,
            set_status_on_exception=False,
        ):
            lease = await self._gateway.open(request, admission)
        if lease.tenant_id != request.tenant_id:
            raise GolDenied("GOL runtime unavailable")
        if contract is not None:
            selectors = (
                *contract.required_symbol_keys,
                *(
                    key
                    for key in (
                        contract.state_symbol_key,
                        contract.derived_symbol_key,
                    )
                    if key
                ),
            )
            if any(lease.symbol(key) is None for key in selectors):
                raise GolDenied("GOL runtime unavailable")
            if contract.allowed_kinds and any(
                symbol.kind not in contract.allowed_kinds
                for symbol in lease.symbols
            ):
                raise GolDenied("GOL runtime unavailable")
        token = _ACTIVE.set(
            BoundCalculation(
                self, request, lease, contract or GolBlockContract(), admission
            )
        )
        try:
            logger.info("gol.runtime.bound")
            yield lease
        finally:
            _ACTIVE.reset(token)

    @asynccontextmanager
    async def protected(
        self, effects: Sequence[GolEffect]
    ) -> AsyncIterator[tuple[str | None, ...]]:
        """A preflight cannot release the exclusion boundary before its actual I/O."""
        bound = _ACTIVE.get()
        if (
            bound is None
            or bound.manager is not self
            or not effects
            or any(
                bound.lease.symbol(effect.target) is None for effect in effects
            )
        ):
            raise GolDenied("GOL effect scope unavailable")
        if any(
            effect.operation in {"write", "execute"}
            and (effect.proposal is None or not effect.instance_id)
            for effect in effects
        ):
            raise GolDenied("GOL business proposal unavailable")
        try:
            canonical = tuple(
                orjson.dumps(
                    effect.model_dump(by_alias=True, exclude_none=True),
                    option=orjson.OPT_SORT_KEYS,
                )
                for effect in effects
            )
        except (TypeError, ValueError):
            raise GolDenied("GOL business proposal unavailable") from None
        held = _EFFECT_ACTIVE.get()
        if held is not None:
            if (
                not held.active
                or held.manager is not self
                or held.owner_task is not asyncio.current_task()
            ):
                raise GolDenied("GOL effect scope unavailable")
            matching: list[str | None] = []
            for effect in canonical:
                index = next(
                    (
                        index
                        for index, prior in enumerate(held.effects)
                        if prior == effect
                    ),
                    None,
                )
                if index is None:
                    raise GolDenied(
                        "An effect cannot extend an active GOL batch"
                    )
                matching.append(held.receipts[index])
            yield tuple(matching)
            return
        with tracer.start_as_current_span(
            "gol.runtime.effect",
            record_exception=False,
            set_status_on_exception=False,
        ):
            async with self._gateway.protected(
                bound.request, bound.lease, bound.admission, effects=effects
            ) as receipts:
                if len(receipts) != len(effects) or any(
                    effect.operation in {"write", "execute"} and not receipt
                    for effect, receipt in zip(effects, receipts, strict=True)
                ):
                    raise GolDenied("GOL effect receipts unavailable")
                held = _HeldBatch(
                    self, canonical, receipts, asyncio.current_task()
                )
                token = _EFFECT_ACTIVE.set(held)
                try:
                    yield receipts
                finally:
                    held.active = False
                    _EFFECT_ACTIVE.reset(token)
