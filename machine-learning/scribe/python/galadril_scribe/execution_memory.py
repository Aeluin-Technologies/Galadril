"""Model-selected memory compression preserves exact evidence in scoped storage."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.toolsets import FunctionToolset

from galadril_scribe.dependencies import Dependencies
from galadril_scribe.execution import ResearchMemory

StateAccess = Callable[[str, JsonValue | None], Awaitable[JsonValue]]


@dataclass
class ExecutionMemory(AbstractCapability[Dependencies]):
    memory: ResearchMemory
    access: StateAccess | None = None
    archived: set[str] = field(default_factory=set)

    async def before_model_request(
        self,
        ctx: RunContext[Dependencies],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        self.memory.observe(ctx.messages)
        ctx.messages[:] = self.memory.project(ctx.messages)
        return replace(
            request_context,
            messages=self.memory.project(request_context.messages),
        )

    def get_instructions(self) -> Callable[[RunContext[Dependencies]], str]:
        return self.memory_instructions

    def memory_instructions(self, ctx: RunContext[Dependencies]) -> str:
        self.memory.observe(ctx.messages)
        pressure = (
            " The working evidence exceeds 100000 characters. Compress secondary leads before further research when possible. Relevant evidence must remain verbatim."
            if sum(map(len, self.memory.originals.values())) > 100000
            else ""
        )
        return (
            "Use memory_inventory to review accumulated tool evidence. Before context grows too large, "
            "use compact_memory to summarize only secondary or unsuccessful leads. Protect relevant "
            "evidence with retain; protected source text remains verbatim with its citations. Never "
            "summarize away a premise, date, quotation, contradiction or source needed for your answer. "
            "Use restore_memory to recover any summarized source before relying on it. Summaries are "
            "navigation aids, not verified evidence. Use save_checkpoint after meaningful milestones."
        ) + pressure

    async def archive(self) -> None:
        if self.access is None:
            return
        for identifier, original in self.memory.originals.items():
            if identifier not in self.archived:
                await self.access("evidence:" + identifier, original)
                self.archived.add(identifier)

    def toolset(
        self, checkpoint: Callable[[], Awaitable[None]]
    ) -> FunctionToolset[Dependencies]:
        async def memory_inventory(ctx: RunContext[Dependencies]) -> str:
            """List source IDs, exact-text sizes and previews before selecting secondary leads to compress."""
            self.memory.observe(ctx.messages)
            return json.dumps(
                [
                    {
                        "id": key,
                        "characters": len(body),
                        "preview": body[:240],
                        "protected": key in self.memory.protected,
                    }
                    for key, body in self.memory.originals.items()
                ],
                ensure_ascii=False,
            )

        async def compact_memory(
            ctx: RunContext[Dependencies],
            secondary: tuple[str, ...],
            retain: tuple[str, ...],
            summary: str,
        ) -> str:
            """Compress selected secondary source IDs with a concise summary. Keep relevant IDs verbatim with retain. Original evidence remains recoverable; protected evidence cannot be compressed."""
            self.memory.observe(ctx.messages)
            await self.archive()
            self.memory.compact(secondary, retain, summary)
            if self.access is not None:
                for identifier in secondary:
                    self.memory.originals.pop(identifier, None)
            return "Secondary leads compressed. Relevant evidence is retained verbatim and originals remain recoverable."

        async def restore_memory(identifier: str) -> str:
            """Recover exact original evidence by its memory source ID before using a summarized finding."""
            if identifier in self.memory.originals:
                return self.memory.restore(identifier)
            if self.access is None:
                raise ValueError("Original evidence unavailable")
            original = await self.access("evidence:" + identifier, None)
            if not isinstance(original, str):
                raise ValueError("Original evidence unavailable")
            self.memory.originals[identifier] = original
            return original

        async def save_checkpoint() -> str:
            """Request a durable checkpoint after the current tool round finishes."""
            await checkpoint()
            return "Checkpoint requested; it will be committed at the next safe model boundary."

        return FunctionToolset[Dependencies](
            tools=[
                memory_inventory,
                compact_memory,
                restore_memory,
                save_checkpoint,
            ]
        )


def checkpoint_messages(
    messages: list[ModelMessage], *, redact: Callable[[str], str] | None = None
) -> str:
    """Persist references instead of binary attachments or expiring storage URLs."""
    prepared: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelResponse) and redact is not None:
            prepared.append(
                replace(
                    message,
                    parts=[
                        replace(part, content=redact(part.content))
                        if isinstance(part, TextPart)
                        else part
                        for part in message.parts
                    ],
                )
            )
            continue
        if not isinstance(message, ModelRequest):
            prepared.append(message)
            continue
        parts = []
        for part in message.parts:
            if isinstance(part, UserPromptPart) and not isinstance(
                part.content, str
            ):
                part = replace(
                    part,
                    content=[
                        value
                        if isinstance(value, str)
                        else "Attachment omitted from checkpoint; retrieve the authorized document again when needed."
                        for value in part.content
                    ],
                )
            if isinstance(part, UserPromptPart) and redact is not None:
                part = replace(
                    part,
                    content=redact(part.content)
                    if isinstance(part.content, str)
                    else [
                        redact(value) if isinstance(value, str) else value
                        for value in part.content
                    ],
                )
            parts.append(part)
        prepared.append(replace(message, parts=parts))
    return ModelMessagesTypeAdapter.dump_json(prepared).decode()
