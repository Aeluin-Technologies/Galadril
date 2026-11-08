"""PydanticAI step persistence uses Gateway's scoped, revisioned state storage."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace

from pydantic import JsonValue, TypeAdapter
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    InMemoryStepStore,
    RunRecord,
    StepEvent,
    ToolEffectRecord,
)

from galadril_scribe.execution_memory import StateAccess, checkpoint_messages


def serialized_state_access(access: StateAccess) -> StateAccess:
    """Keep one generation's compare-and-swap writes ordered without retrying denials."""
    lock = asyncio.Lock()

    async def serialized(key: str, value: JsonValue | None) -> JsonValue:
        async with lock:
            return await access(key, value)

    return serialized


class GatewayStepStore(InMemoryStepStore):
    def __init__(
        self,
        access: StateAccess,
        on_snapshot: Callable[[ContinuableSnapshot], Awaitable[None]],
        *,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(max_snapshots_per_run=2)
        self.redact = redact
        self.access = access
        self.on_snapshot = on_snapshot
        self.index_lock = asyncio.Lock()

    @staticmethod
    def key(*parts: str) -> str:
        return "native:" + hashlib.sha256("/".join(parts).encode()).hexdigest()

    async def index(self, name: str, identifier: str) -> None:
        async with self.index_lock:
            membership = self.key(name, "member", identifier)
            if await self.access(membership, None) is True:
                return
            count_key = self.key(name, "count")
            count = await self.access(count_key, None)
            size = (
                count
                if isinstance(count, int) and not isinstance(count, bool)
                else 0
            )
            page_key = self.key(name, str(size // 128))
            page = await self.access(page_key, None)
            entries = (
                [item for item in page if isinstance(item, str)]
                if isinstance(page, list)
                else []
            )
            if identifier in entries:
                return
            entries.append(identifier)
            await self.access(page_key, entries)
            await self.access(count_key, size + 1)
            await self.access(membership, True)

    async def indexed(self, name: str) -> list[str]:
        count = await self.access(self.key(name, "count"), None)
        size = (
            count
            if isinstance(count, int) and not isinstance(count, bool)
            else 0
        )
        if not 0 <= size <= 1000000:
            raise ValueError("Invalid native state index")
        result: list[str] = []
        for page in range((size + 127) // 128):
            entries = await self.access(self.key(name, str(page)), None)
            if isinstance(entries, list):
                result.extend(item for item in entries if isinstance(item, str))
        return result

    async def register_run(self, record: RunRecord) -> None:
        if await self.get_run(run_id=record.run_id) is not None:
            raise ValueError("Native run is already registered")
        await self.access(
            self.key("run", record.run_id),
            TypeAdapter(RunRecord).dump_json(record).decode(),
        )
        await self.index("runs", record.run_id)

    async def get_run(self, *, run_id: str) -> RunRecord | None:
        value = await self.access(self.key("run", run_id), None)
        return (
            TypeAdapter(RunRecord).validate_json(value)
            if isinstance(value, str)
            else None
        )

    async def list_runs(
        self,
        *,
        parent_run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> list[RunRecord]:
        result: list[RunRecord] = []
        for identifier in await self.indexed("runs"):
            record = await self.get_run(run_id=identifier)
            if (
                record
                and (
                    parent_run_id is None
                    or record.parent_run_id == parent_run_id
                )
                and (
                    conversation_id is None
                    or record.conversation_id == conversation_id
                )
            ):
                result.append(record)
        return sorted(result, key=lambda record: record.started_at)

    async def append_event(self, event: StepEvent) -> None:
        encoded = TypeAdapter(StepEvent).dump_json(event).decode()
        identifier = hashlib.sha256(
            (event.idempotency_key or encoded).encode()
        ).hexdigest()
        await self.access(self.key("event", identifier), encoded)
        await self.index("events:" + event.run_id, identifier)

    async def list_events(self, *, run_id: str) -> list[StepEvent]:
        result: list[StepEvent] = []
        for identifier in await self.indexed("events:" + run_id):
            value = await self.access(self.key("event", identifier), None)
            if isinstance(value, str):
                result.append(TypeAdapter(StepEvent).validate_json(value))
        return result

    async def save_snapshot(self, snapshot: ContinuableSnapshot) -> None:
        snapshot = replace(
            snapshot,
            messages=ModelMessagesTypeAdapter.validate_json(
                checkpoint_messages(snapshot.messages, redact=self.redact)
            ),
        )
        encoded = TypeAdapter(ContinuableSnapshot).dump_json(snapshot).decode()
        await self.access(self.key("latest", snapshot.run_id), encoded)
        if snapshot.state == "complete":
            await self.access(self.key("complete", snapshot.run_id), encoded)
        await super().save_snapshot(snapshot)
        await self.on_snapshot(snapshot)

    async def latest_snapshot(
        self, *, run_id: str, include_interrupted: bool = False
    ) -> ContinuableSnapshot | None:
        value = await self.access(
            self.key("latest" if include_interrupted else "complete", run_id),
            None,
        )
        return (
            TypeAdapter(ContinuableSnapshot).validate_json(value)
            if isinstance(value, str)
            else None
        )

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        await self.access(
            self.key("effect", record.run_id, record.tool_call_id),
            TypeAdapter(ToolEffectRecord).dump_json(record).decode(),
        )
        await self.index("effects:" + record.run_id, record.tool_call_id)

    async def get_tool_effect(
        self, *, run_id: str, tool_call_id: str
    ) -> ToolEffectRecord | None:
        value = await self.access(
            self.key("effect", run_id, tool_call_id), None
        )
        return (
            TypeAdapter(ToolEffectRecord).validate_json(value)
            if isinstance(value, str)
            else None
        )

    async def list_unresolved_tool_effects(
        self, *, run_id: str
    ) -> list[ToolEffectRecord]:
        result: list[ToolEffectRecord] = []
        for identifier in await self.indexed("effects:" + run_id):
            record = await self.get_tool_effect(
                run_id=run_id, tool_call_id=identifier
            )
            if record is not None and record.status == "started":
                result.append(record)
        return result
