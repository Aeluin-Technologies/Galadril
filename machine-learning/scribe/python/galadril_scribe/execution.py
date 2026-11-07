"""Native execution policy, incremental output protection and reversible memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import UsageLimits

if TYPE_CHECKING:
    from galadril_scribe.builtin import SecretPolicy
    from galadril_scribe.contracts import RunRequest, Settings


class ExecutionState(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    version: Literal[1] = 1
    messages: str = Field(max_length=360000)
    requests: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    web_closed: bool = False
    team_reports: str = Field(default='{"reports":[]}', max_length=65536)
    safe: bool
    cost_usd: Decimal | None = Field(default=None, ge=0)
    mission: str = Field(max_length=32768)


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    limits: UsageLimits
    timeout_seconds: int

    @classmethod
    def for_request(
        cls, settings: Settings, request: RunRequest
    ) -> ExecutionBudget:
        research = request.execution_mode == "research"
        return cls(
            limits=UsageLimits(
                request_limit=(
                    settings.research_model_requests
                    if research
                    else settings.max_model_requests
                ),
                tool_calls_limit=None,
                cost_limit=settings.research_cost_limit_usd
                if research
                else settings.chat_cost_limit_usd,
                total_tokens_limit=(
                    settings.research_token_limit
                    if research
                    else settings.chat_token_limit
                ),
            ),
            timeout_seconds=(
                settings.research_timeout_seconds
                if research
                else settings.run_timeout_seconds
            ),
        )


class StreamingText:
    """Hold incomplete credentials before releasing independently sanitized text."""

    __slots__ = ("policy", "pending")

    def __init__(self, policy: SecretPolicy) -> None:
        self.policy = policy
        self.pending = ""

    def feed(self, value: str, *, final: bool = False) -> str:
        self.pending += value
        if len(self.pending) > 65536:
            raise ValueError(
                "Unbroken output exceeds streaming protection limit"
            )
        if final:
            ready, self.pending = self.pending, ""
            return self.policy.public_text(ready)
        # A lexical token may contain a credential of arbitrary length. PEM
        # material also spans lines, so keep its header and body together.
        boundary = max(
            (match.end() for match in re.finditer(r"\s+", self.pending)),
            default=0,
        )
        pem = self.pending.find("-----BEGIN")
        if pem >= 0:
            boundary = min(boundary, pem)
        for secret in (*self.policy.secrets, *self.policy._dynamic):
            for length in range(1, min(len(secret), len(self.pending)) + 1):
                if self.pending.endswith(secret[:length]):
                    boundary = min(boundary, len(self.pending) - length)
            start = self.pending.find(secret) if secret else -1
            if 0 <= start < boundary < start + len(secret):
                boundary = start
        ready, self.pending = self.pending[:boundary], self.pending[boundary:]
        return self.policy.public_text(ready)


class ResearchMemory:
    """Keep exact source bodies separately from model-authored secondary summaries."""

    __slots__ = ("originals", "summaries", "protected")

    def __init__(self) -> None:
        self.originals: dict[str, str] = {}
        self.summaries: dict[str, str] = {}
        self.protected: set[str] = set()

    def observe(self, messages: list[ModelMessage]) -> None:
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if (
                        isinstance(part, ToolReturnPart)
                        and part.tool_call_id not in self.summaries
                    ):
                        metadata = (
                            part.metadata
                            if isinstance(part.metadata, dict)
                            else {}
                        )
                        if metadata.get("galadril.memory.v1") == "protected":
                            self.protected.add(part.tool_call_id)
                        if metadata.get("galadril.memory.v1") == "secondary":
                            self.summaries[part.tool_call_id] = (
                                part.model_response_str()
                            )
                        else:
                            self.originals.setdefault(
                                part.tool_call_id, part.model_response_str()
                            )

    def compact(
        self, secondary: tuple[str, ...], retain: tuple[str, ...], summary: str
    ) -> None:
        if (
            not secondary
            or not summary.strip()
            or len(summary) > 8192
            or len(secondary) + len(retain) > 128
            or set(secondary) & (self.protected | set(retain))
            or any(key not in self.originals for key in (*secondary, *retain))
        ):
            raise ValueError("Memory selection is unavailable or protected")
        self.protected.update(retain)
        for key in secondary:
            self.summaries[key] = (
                f"Secondary lead summary (unverified synthesis): {summary}\nOriginal evidence reference: {key}. Use restore_memory to recover the exact source."
            )

    def project(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if not self.summaries and not self.protected:
            return messages

        def project_part(part: ToolReturnPart) -> ToolReturnPart:
            metadata = (
                dict(part.metadata) if isinstance(part.metadata, dict) else {}
            )
            metadata["galadril.memory.v1"] = (
                "secondary"
                if part.tool_call_id in self.summaries
                else "protected"
            )
            return replace(
                part,
                content=self.summaries.get(part.tool_call_id, part.content),
                metadata=metadata,
            )

        return [
            replace(
                message,
                parts=[
                    project_part(part)
                    if isinstance(part, ToolReturnPart)
                    and (
                        part.tool_call_id in self.summaries
                        or part.tool_call_id in self.protected
                    )
                    else part
                    for part in message.parts
                ],
            )
            if isinstance(message, ModelRequest)
            else message
            for message in messages
        ]

    def restore(self, identifier: str) -> str:
        return self.originals[identifier]


def pack_text(value: str) -> str:
    """Compress persisted text without summarizing quotations or source metadata."""
    import base64
    import zlib

    encoded = value.encode()
    if len(encoded) < 16384:
        return "t1:" + value
    return "z1:" + base64.b64encode(zlib.compress(encoded)).decode("ascii")


def unpack_text(value: str) -> str:
    """Bound decompression before allocating a model-history checkpoint."""
    import base64
    import zlib

    if value.startswith("t1:"):
        return value[3:]
    if not value.startswith("z1:"):
        return value
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(
        base64.b64decode(value[3:], validate=True), 8 * 1024 * 1024 + 1
    )
    if (
        len(decoded) > 8 * 1024 * 1024
        or decoder.unconsumed_tail
        or not decoder.eof
        or decoder.unused_data
    ):
        raise ValueError("Checkpoint exceeds decompression limits")
    return decoded.decode("utf-8")


def safe_to_replay(messages: list[ModelMessage]) -> bool:
    """Replay known read operations; unknown effects require an explicit review."""
    pending: dict[str, str] = {}
    for message in messages:
        for part in message.parts:
            if isinstance(message, ModelResponse) and isinstance(
                part, ToolCallPart
            ):
                pending[part.tool_call_id] = part.tool_name
            elif isinstance(message, ModelRequest) and isinstance(
                part, ToolReturnPart
            ):
                pending.pop(part.tool_call_id, None)
    retryable = {
        "web_search",
        "date_interval",
        "document_search",
        "search",
        "search_evidence",
        "read_file",
        "read_document",
        "memory_inventory",
        "restore_memory",
        "read_plan",
        "search_conversation_history",
    }
    return all(name in retryable for name in pending.values())
