"""Background execution owns model work independently from HTTP consumers."""

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from galadril_scribe.contracts import GenerationRequest
from galadril_scribe.interactions import AnswerSubmission, InteractionFailure

if TYPE_CHECKING:
    from galadril_scribe.runtime import Runtime

logger = structlog.get_logger(__name__)
_CONTENT_BATCH_BYTES = 3072
_CONTENT_FRAGMENT_CHARS = _CONTENT_BATCH_BYTES // 6


def lifespan(
    runtime: "Runtime",
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def context(app: FastAPI) -> AsyncIterator[None]:
        yield
        tasks = [task for _, task in tuple(runtime.jobs.values())]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return context


async def deliver(
    runtime: "Runtime",
    request: GenerationRequest,
    sequence: int,
    kind: str,
    content: str = "",
) -> None:
    endpoint = (
        runtime.settings.gateway_tools_url.removesuffix("/tools") + "/events"
    )
    for attempt in range(3):
        try:
            response = await runtime.client.post(
                endpoint,
                headers={
                    "authorization": "Bearer "
                    + request.capability.get_secret_value()
                },
                json={
                    "generationId": request.id,
                    "sequence": sequence,
                    "kind": kind,
                    "content": content,
                },
                timeout=10,
            )
            if response.status_code == 200:
                return
            logger.warning(
                "scribe.callback.rejected",
                generation_id=request.id,
                sequence=sequence,
                kind=kind,
                status_code=response.status_code,
            )
            if response.status_code < 500:
                raise RuntimeError("Generation is no longer authorized")
        except httpx.TransportError:
            pass
        if attempt < 2:
            await asyncio.sleep(0.25 * (attempt + 1))
    raise RuntimeError("Generation persistence unavailable")


async def execute(runtime: "Runtime", request: GenerationRequest) -> None:
    worker = asyncio.current_task()

    async def heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(2)
                await deliver(runtime, request, 0, "heartbeat")
        except Exception:
            if worker is not None:
                worker.cancel()

    monitor = asyncio.create_task(heartbeat())
    sequence = 0
    lock = asyncio.Lock()
    pending: list[str] = []
    pending_bytes = 0

    async def persist(kind: str, content: str = "") -> None:
        nonlocal sequence
        # Advance only after acknowledgement; a failed delivery keeps its cursor
        # for an identical retry or an explicit lease failure.
        await deliver(runtime, request, sequence + 1, kind, content)
        sequence += 1

    async def flush() -> None:
        nonlocal pending_bytes
        if pending:
            await persist("content", "".join(pending))
            pending.clear()
            pending_bytes = 0

    async def emit(kind: str, content: str = "") -> None:
        """Reserve callback envelope space within a 4 KiB Gateway body limit."""
        nonlocal pending_bytes
        async with lock:
            if kind == "content":
                # One JSON character can expand to six bytes after escaping.
                for offset in range(0, len(content), _CONTENT_FRAGMENT_CHARS):
                    fragment = content[
                        offset : offset + _CONTENT_FRAGMENT_CHARS
                    ]
                    encoded_bytes = (
                        len(json.dumps(fragment, ensure_ascii=False).encode())
                        - 2
                    )
                    if pending_bytes + encoded_bytes > _CONTENT_BATCH_BYTES:
                        await flush()
                    pending.append(fragment)
                    pending_bytes += encoded_bytes
                    if pending_bytes >= _CONTENT_BATCH_BYTES:
                        await flush()
            else:
                await flush()
                await persist(kind, content)

    async def interval_flush() -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                async with lock:
                    await flush()
        except Exception:
            if worker is not None:
                worker.cancel()

    flusher = asyncio.create_task(interval_flush())

    async def progress(operation: str) -> None:
        await emit("tool", operation)

    runtime.progress[request.id] = progress
    try:
        await deliver(runtime, request, 0, "heartbeat")
        async for chunk in runtime.stream(request):
            await emit(chunk.kind, chunk.content)
    except asyncio.CancelledError:
        logger.info("scribe.background.cancelled")
    except Exception:
        logger.warning("scribe.background.failed")
        try:
            await deliver(runtime, request, sequence + 1, "failed")
        except Exception:
            logger.warning("scribe.background.terminal_unavailable")
    finally:
        monitor.cancel()
        flusher.cancel()
        await asyncio.gather(monitor, flusher, return_exceptions=True)


def install_generations(app: FastAPI, runtime: "Runtime") -> None:
    @app.post("/generations")
    async def submit(incoming: Request) -> JSONResponse:
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                async for part in incoming.stream():
                    if len(body) + len(part) > 262144:
                        raise HTTPException(413, "Request exceeds limit")
                    body.extend(part)
        except TimeoutError:
            raise HTTPException(408, "Request timed out") from None
        try:
            request = GenerationRequest.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Request rejected") from None
        key = request.capability.get_secret_value()
        existing = runtime.jobs.get(request.id)
        if existing is not None:
            if not hmac.compare_digest(existing[0], key):
                raise HTTPException(409, "Generation unavailable")
            return JSONResponse({"id": request.id}, status_code=202)
        try:
            runtime.select_model(request)
        except ValueError:
            raise HTTPException(422, "Model selection rejected") from None
        try:
            runtime.reserve()
        except RuntimeError:
            raise HTTPException(429, "Runtime capacity exceeded") from None
        task = asyncio.create_task(execute(runtime, request))
        runtime.jobs[request.id] = (key, task)

        def finished(completed: asyncio.Task[None]) -> None:
            runtime.jobs.pop(request.id, None)
            runtime.progress.pop(request.id, None)
            runtime.release()
            if not completed.cancelled() and completed.exception() is not None:
                logger.warning("scribe.background.task_failed")

        task.add_done_callback(finished)
        return JSONResponse({"id": request.id}, status_code=202)

    @app.post("/generations/{identifier}/answers")
    async def answer(identifier: str, incoming: Request) -> dict[str, str]:
        job = runtime.jobs.get(identifier)
        header = incoming.headers.get("authorization", "")
        token = header.removeprefix("Bearer ")
        if (
            not header.startswith("Bearer ")
            or job is None
            or not hmac.compare_digest(job[0], token)
        ):
            raise HTTPException(404, "Generation unavailable")
        body = bytearray()
        try:
            async with asyncio.timeout(15):
                async for part in incoming.stream():
                    if len(body) + len(part) > 65536:
                        raise HTTPException(413, "Answer exceeds limit")
                    body.extend(part)
        except TimeoutError:
            raise HTTPException(408, "Answer timed out") from None
        try:
            submission = AnswerSubmission.model_validate_json(body)
            runtime.interactions.answer(identifier, token, submission)
        except ValidationError:
            raise HTTPException(422, "Answer rejected") from None
        except InteractionFailure as error:
            status = {"unavailable": 404, "invalid": 422, "conflict": 409}.get(
                error.code, 422
            )
            raise HTTPException(status, "Answer rejected") from None
        return {"status": "accepted"}

    @app.post("/generations/{identifier}/cancel")
    async def cancel(identifier: str, incoming: Request) -> dict[str, str]:
        job = runtime.jobs.get(identifier)
        token = incoming.headers.get("authorization", "").removeprefix(
            "Bearer "
        )
        if job is None or not hmac.compare_digest(job[0], token):
            raise HTTPException(404, "Generation unavailable")
        job[1].cancel()
        await asyncio.gather(job[1], return_exceptions=True)
        return {"status": "cancelled"}
