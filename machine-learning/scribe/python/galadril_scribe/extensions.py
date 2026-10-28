"""Trusted tool composition keeps deployment code out of model arguments."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Self

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
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
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
_TOOL_RECOVERY_SECONDS = 15.0
logger = structlog.get_logger(__name__)


def _prefix(identifier: str) -> str:
    return identifier.replace(".", "_").replace("-", "_")


@dataclass(slots=True)
class _Availability:
    entered: bool = False
    unavailable: bool = False
    retry_at: float = 0.0
    disabled: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class AvailableToolset(WrapperToolset[Dependencies]):
    """Keep optional failures local to one run while preserving native control flow."""

    capability_id: str
    timeout_seconds: float | None = None
    reconnect_after_failure: bool = False
    _state: _Availability = field(default_factory=_Availability)
    _factory: Callable[[], AbstractToolset[Dependencies]] | None = None

    def _failed(self, error: Exception) -> None:
        self._state.unavailable = True
        self._state.retry_at = monotonic() + _TOOL_RECOVERY_SECONDS
        logger.warning(
            "scribe.tool.discovery_failed",
            capability_id=self.capability_id,
            error_type=type(error).__name__,
        )

    async def __aenter__(self) -> Self:
        if self._state.entered or (
            self._state.unavailable and monotonic() < self._state.retry_at
        ):
            return self
        try:
            if self._state.unavailable and self._factory is not None:
                self.wrapped = self._factory()
            async with asyncio.timeout(
                self.timeout_seconds or _TOOL_TIMEOUT_SECONDS
            ):
                await self.wrapped.__aenter__()
            self._state.entered = True
            self._state.unavailable = False
        except (AgentRunError, UserError):
            raise
        except Exception as error:
            self._failed(error)
        return self

    async def __aexit__(self, *args: object) -> bool | None:
        if not self._state.entered:
            return False
        try:
            return await self.wrapped.__aexit__(*args)
        except (AgentRunError, UserError):
            raise
        except Exception as error:
            self._failed(error)
            return False
        finally:
            self._state.entered = False

    async def get_tools(
        self, ctx: RunContext[Dependencies]
    ) -> dict[str, ToolsetTool[Dependencies]]:
        if self._state.unavailable and monotonic() >= self._state.retry_at:
            await self.__aexit__(None, None, None)
            await self.__aenter__()
        if not self._state.unavailable:
            try:
                async with asyncio.timeout(
                    self.timeout_seconds or _TOOL_TIMEOUT_SECONDS
                ):
                    tools = await self.wrapped.get_tools(ctx)
                if not self._state.disabled:
                    return tools
                now = monotonic()
                for name, retry_at in tuple(self._state.disabled.items()):
                    if retry_at <= now:
                        del self._state.disabled[name]
                return {
                    name: tool
                    for name, tool in tools.items()
                    if name not in self._state.disabled
                }
            except (AgentRunError, UserError):
                raise
            except Exception as error:
                self._failed(error)
        if ctx.deps.warning is not None:
            await ctx.deps.warning("tool")
        return {}

    def _instruction_children(self) -> Sequence[AbstractToolset[Dependencies]]:
        return () if self._state.unavailable else (self.wrapped,)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[Dependencies],
        tool: ToolsetTool[Dependencies],
    ) -> object:
        retry_at = self._state.disabled.get(name)
        if self._state.unavailable or (
            retry_at is not None and retry_at > monotonic()
        ):
            raise ToolFailed(
                "This operation is unavailable. Continue with other authorized tools and do not assume success."
            )
        with trace.get_tracer("galadril.scribe").start_as_current_span(
            "scribe.extension.call",
            attributes={"capability.id": self.capability_id},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                async with asyncio.timeout(
                    self.timeout_seconds or _TOOL_TIMEOUT_SECONDS
                ):
                    return await self.wrapped.call_tool(
                        name, tool_args, ctx, tool
                    )
            except (
                AgentRunError,
                ApprovalRequired,
                CallDeferred,
                UserError,
            ):
                raise
            except Exception as error:
                # MCP retries describe upstream failures; native retries also repair tool input.
                if (
                    isinstance(error, (ModelRetry, ToolRetryError))
                    and not self.reconnect_after_failure
                ):
                    raise
                self._state.disabled[name] = (
                    monotonic() + _TOOL_RECOVERY_SECONDS
                )
                if self.reconnect_after_failure:
                    self._state.unavailable = True
                    self._state.retry_at = self._state.disabled[name]
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
        result: list[AbstractToolset[Dependencies]] = []
        for identifier in identifiers:
            factory = self._extensions[identifier].factory
            try:
                wrapped = factory()
                bounded = AvailableToolset(
                    wrapped,
                    identifier,
                    reconnect_after_failure=self._extensions[
                        identifier
                    ].capability.kind
                    is CapabilityKind.MCP,
                    _factory=factory,
                )
            except Exception as error:
                bounded = AvailableToolset(
                    FunctionToolset(),
                    identifier,
                    reconnect_after_failure=self._extensions[
                        identifier
                    ].capability.kind
                    is CapabilityKind.MCP,
                    _factory=factory,
                )
                bounded._failed(error)
            result.append(bounded.prefixed(_prefix(identifier)))
        return result
