"""ConversationSearch sees only Gateway-authorized published conversation text."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai_harness.step_persistence import RunRecord


class PublishedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(max_length=32768)


class PublishedHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: tuple[PublishedMessage, ...] = Field(max_length=64)


class ConversationSource:
    """The model cannot choose a tenant, project, owner or private snapshot scope."""

    __slots__ = ("query",)

    def __init__(
        self, query: Callable[[str | None], Awaitable[JsonValue]]
    ) -> None:
        self.query = query

    async def list_runs(self) -> list[RunRecord]:
        value = await self.query(None)
        if not isinstance(value, list):
            raise ValueError("Conversation memory unavailable")
        return [
            RunRecord(run_id=identifier, conversation_id=identifier)
            for identifier in value
            if isinstance(identifier, str)
        ]

    async def run_history(self, *, run_id: str) -> list[ModelMessage]:
        result = PublishedHistory.model_validate(await self.query(run_id))
        return [
            ModelRequest(parts=[UserPromptPart(message.content)])
            if message.role == "user"
            else ModelResponse(parts=[TextPart(message.content)])
            for message in result.messages
        ]
