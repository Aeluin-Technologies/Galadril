"""Trusted tool composition keeps deployment code out of model arguments."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import structlog
from galadril_pipeline.capabilities import (
    Capability,
    CapabilityCatalog,
    CapabilityKind,
    CapabilityTarget,
)
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic_ai import RunContext
from pydantic_ai.exceptions import (
    AgentRunError,
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    ToolFailed,
    ToolRetryError,
    UserError,
)
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from galadril_scribe.dependencies import Dependencies


@dataclass(frozen=True, slots=True)
class ToolExtension:
    """Factories create run-owned toolsets without receiving delegated credentials."""

    capability: Capability
    factory: Callable[[], AbstractToolset[Dependencies]]

    def __post_init__(self) -> None:
        if (
            self.capability.kind
            not in {CapabilityKind.MCP, CapabilityKind.ALGORITHM}
            or CapabilityTarget.SCRIBE not in self.capability.targets
            or self.capability.execution != "registered"
        ):
            raise ValueError("Scribe extensions must be MCP or algorithm tools")


_TOOL_TIMEOUT_SECONDS = 30.0
logger = structlog.get_logger(__name__)


def _prefix(identifier: str) -> str:
    return identifier.replace(".", "_").replace("-", "_")


@dataclass(slots=True)
class _BoundedToolset(WrapperToolset[Dependencies]):
    """Bounds extension calls without recording arguments or raw exceptions in OTLP."""

    capability_id: str

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[Dependencies],
        tool: ToolsetTool[Dependencies],
    ) -> object:
        with trace.get_tracer("galadril.scribe").start_as_current_span(
            "scribe.extension.call",
            attributes={"capability.id": self.capability_id},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                async with asyncio.timeout(_TOOL_TIMEOUT_SECONDS):
                    return await self.wrapped.call_tool(
                        name, tool_args, ctx, tool
                    )
            except (
                AgentRunError,
                ApprovalRequired,
                CallDeferred,
                ModelRetry,
                ToolRetryError,
                UserError,
            ):
                raise
            except Exception as error:
                span.set_status(StatusCode.ERROR)
                logger.warning(
                    "scribe.extension.failed",
                    capability_id=self.capability_id,
                    error_type=type(error).__name__,
                )
                if ctx.deps.warning is not None:
                    await ctx.deps.warning("tool")
                raise ToolFailed(
                    "The operation is unavailable or incomplete. Do not retry it automatically "
                    "or assume success. Continue with independent authorized steps and "
                    "explain what could not be completed."
                ) from None


class ToolRegistry:
    """Validates selections before constructing any external service clients."""

    __slots__ = ("_extensions", "_catalog", "capabilities")

    def __init__(self, extensions: Sequence[ToolExtension]) -> None:
        self._extensions = {item.capability.id: item for item in extensions}
        if len(self._extensions) != len(extensions):
            raise ValueError("Extension IDs must be unique")
        if len({_prefix(item.capability.id) for item in extensions}) != len(
            extensions
        ):
            raise ValueError("Extension tool prefixes must be unique")
        self.capabilities = tuple(
            item.capability.model_copy(deep=True) for item in extensions
        )
        self._catalog = CapabilityCatalog(capabilities=self.capabilities)

    def validate(
        self, identifiers: tuple[str, ...], *, model: str | None = None
    ) -> None:
        self._catalog.validate_selection(
            identifiers, target=CapabilityTarget.SCRIBE, model=model
        )

    def toolsets(
        self, identifiers: tuple[str, ...], *, model: str | None = None
    ) -> list[AbstractToolset[Dependencies]]:
        self.validate(identifiers, model=model)
        return [
            _BoundedToolset(
                self._extensions[identifier].factory(), identifier
            ).prefixed(_prefix(identifier))
            for identifier in identifiers
        ]
