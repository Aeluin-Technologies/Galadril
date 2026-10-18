"""Bounded Python calculations using Monty's isolated interpreter workers."""

import ast
import asyncio
import base64
import sys
from dataclasses import dataclass, field
from importlib.metadata import distribution
from pathlib import Path
from types import TracebackType
from typing import cast

import httpx
from galadril_sandbox.contracts import (
    ExecutionRequest,
    ExecutionResult,
    InstalledLibrary,
    Library,
)
from pydantic import ValidationError
from pydantic_monty import AsyncMonty, MontyError

from galadril_scribe.scientific import Plot, scientific_source


class SandboxFailure(RuntimeError):
    """A snippet failed without exposing its source or host diagnostics."""


@dataclass(frozen=True, slots=True)
class SandboxResult:
    text: str
    image: bytes | None = field(default=None, repr=False)
    libraries: tuple[InstalledLibrary, ...] = ()


def runtime_binary() -> str:
    """Use the pinned wheel's install prefix; OCI layers omit its RECORD file."""
    runtime = distribution("pydantic-monty-runtime")
    name = "monty.exe" if sys.platform == "win32" else "monty"
    binary = Path(str(runtime.locate_file(f"../../../bin/{name}")))
    if not binary.is_file():
        raise SandboxFailure("Sandbox runtime unavailable")
    return str(binary)


class Sandbox:
    __slots__ = ("pool", "client", "socket_path")

    def __init__(
        self,
        *,
        socket_path: str | None = None,
    ) -> None:
        """Resolve the pinned wheel's worker without relying on a host PATH."""
        self.client: httpx.AsyncClient | None = None
        self.socket_path = socket_path
        self.pool = (
            AsyncMonty(
                binary_path=runtime_binary(),
                max_processes=1,
                checkout_timeout=0.5,
                request_timeout=3,
                max_checkouts_per_worker=64,
            )
            if socket_path is None
            else None
        )

    async def __aenter__(self) -> "Sandbox":
        if self.pool is not None:
            await self.pool.__aenter__()
        elif self.socket_path is not None:
            self.client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=self.socket_path),
                base_url="http://python-sandbox/",
                trust_env=False,
                follow_redirects=False,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.pool is not None:
            await self.pool.__aexit__(exc_type, exc, traceback)
        if self.client is not None:
            await self.client.aclose()

    async def execute(self, code: str) -> str:
        return (await self.evaluate(code)).text

    async def evaluate(
        self, code: str, libraries: tuple[Library, ...] = ()
    ) -> SandboxResult:
        """Discard each session, including failures, to isolate actors and budgets."""
        if not code.strip() or len(code.encode()) > 8192:
            raise SandboxFailure("Sandbox input exceeds limit")
        if self.socket_path is not None:
            return await self._remote(code, libraries)
        if any(library not in {"numpy", "matplotlib"} for library in libraries):
            raise SandboxFailure(
                "Library installation requires an isolated Python worker"
            )
        if self.pool is None:
            raise SandboxFailure("Sandbox unavailable")
        output_parts: list[str] = []
        output_bytes = 0

        def collect(stream: str, text: str) -> None:
            nonlocal output_bytes
            output_bytes += len(text.encode())
            if output_bytes > 65536:
                raise SandboxFailure("Sandbox output exceeds limit")
            output_parts.append(text)

        try:
            plot = Plot()
            source, bindings = scientific_source(code, plot)
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
                output: object = await session.feed_run(
                    source, external_lookup=bindings, print_callback=collect
                )
                if output is not None:
                    collect("stdout", str(output))
                text = "".join(output_parts)
            image = await asyncio.to_thread(plot.render)
        except (
            MontyError,
            ValueError,
            SyntaxError,
            RecursionError,
            OverflowError,
        ):
            raise SandboxFailure("Sandbox execution failed") from None
        if len(text.encode()) > 65536:
            raise SandboxFailure("Sandbox output exceeds limit")
        return SandboxResult(text=text, image=image)

    async def _remote(
        self, code: str, libraries: tuple[Library, ...]
    ) -> SandboxResult:
        if self.client is None:
            raise SandboxFailure("Sandbox unavailable")
        try:
            roots = {
                "numpy": "numpy",
                "matplotlib": "matplotlib",
                "pandas": "pandas",
                "scipy": "scipy",
                "sympy": "sympy",
                "PIL": "pillow",
                "sklearn": "scikit-learn",
            }
            selected = set(libraries)
            for node in ast.walk(ast.parse(code)):
                imports = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module]
                    if isinstance(node, ast.ImportFrom) and node.module
                    else []
                )
                for name in imports:
                    if name.split(".")[0] in roots:
                        selected.add(cast(Library, roots[name.split(".")[0]]))
            request = ExecutionRequest(
                code=code, libraries=tuple(sorted(selected))
            )
            body = bytearray()
            async with self.client.stream(
                "POST",
                "/execute",
                json=request.model_dump(mode="json"),
                timeout=75,
            ) as response:
                if response.status_code != 200:
                    raise SandboxFailure("Sandbox unavailable")
                async for part in response.aiter_bytes():
                    if len(body) + len(part) > 524288:
                        raise SandboxFailure("Sandbox output exceeds limit")
                    body.extend(part)
            result = ExecutionResult.model_validate_json(body)
            if result.status != "completed":
                raise SandboxFailure("Sandbox execution failed")
            return SandboxResult(
                text=result.text,
                image=base64.b64decode(result.image, validate=True)
                if result.image is not None
                else None,
                libraries=result.libraries,
            )
        except (
            httpx.HTTPError,
            ValueError,
            SyntaxError,
            RecursionError,
            ValidationError,
        ):
            raise SandboxFailure("Sandbox execution failed") from None
