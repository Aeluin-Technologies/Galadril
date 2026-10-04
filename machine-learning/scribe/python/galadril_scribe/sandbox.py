"""Bounded Python calculations using Monty's isolated interpreter workers."""

from importlib.metadata import distribution
from types import TracebackType

from pydantic_monty import AsyncMonty, MontyError


class SandboxFailure(RuntimeError):
    """A snippet failed without exposing its source or host diagnostics."""


class Sandbox:
    __slots__ = ("pool",)

    def __init__(self) -> None:
        """Resolve the pinned wheel's worker without relying on a host PATH."""
        runtime = distribution("pydantic-monty-runtime")
        binaries = [
            runtime.locate_file(file)
            for file in runtime.files or ()
            if file.name in {"monty", "monty.exe"}
        ]
        if len(binaries) != 1:
            raise SandboxFailure("Sandbox runtime unavailable")
        self.pool = AsyncMonty(
            binary_path=str(binaries[0]),
            max_processes=1,
            checkout_timeout=0.5,
            request_timeout=3,
            max_checkouts_per_worker=64,
        )

    async def __aenter__(self) -> "Sandbox":
        await self.pool.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.pool.__aexit__(exc_type, exc, traceback)

    async def execute(self, code: str) -> str:
        """Discard each session, including failures, to isolate actors and budgets."""
        if not code.strip() or len(code.encode()) > 8192:
            raise SandboxFailure("Sandbox input exceeds limit")
        try:
            async with self.pool.checkout(
                limits={
                    "max_memory": 16 * 1024 * 1024,
                    "max_feed_duration_secs": 0.5,
                    "max_turn_duration_secs": 0.5,
                    "max_recursion_depth": 100,
                    "max_suspensions": 16,
                    "max_total_sleep_secs": 0,
                },
                os_policy={"sleep": "zero"},
            ) as session:
                output: object = await session.feed_run(code)
                text = str(output)
        except MontyError:
            raise SandboxFailure("Sandbox execution failed") from None
        if len(text.encode()) > 65536:
            raise SandboxFailure("Sandbox output exceeds limit")
        return text
