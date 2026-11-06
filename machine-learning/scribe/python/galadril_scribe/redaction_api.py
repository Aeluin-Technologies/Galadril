"""Only workload-authorized callers may submit private bytes for native redaction."""

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from galadril_scribe.redaction import (
    RedactionCapacity,
    RedactionFailure,
    RedactionRuntime,
)
from galadril_scribe.redaction_contracts import MAX_WIRE_BYTES


def install_redaction(app: FastAPI, runtime: RedactionRuntime) -> None:
    inflight = 0

    @app.post("/documents/redact")
    async def redact(incoming: Request) -> Response:
        nonlocal inflight
        if inflight >= 2:
            raise HTTPException(429, "Redaction capacity exceeded")
        inflight += 1
        try:
            return await execute(incoming)
        finally:
            inflight -= 1

    async def execute(incoming: Request) -> Response:
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                async for part in incoming.stream():
                    if len(body) + len(part) > MAX_WIRE_BYTES:
                        raise HTTPException(
                            413, "Redaction request exceeds limit"
                        )
                    body.extend(part)
            payload = bytes(body)
        except TimeoutError:
            raise HTTPException(408, "Redaction request timed out") from None
        finally:
            body.clear()
        try:
            result = await runtime.run_bytes(payload)
        except RedactionCapacity:
            raise HTTPException(429, "Redaction capacity exceeded") from None
        except RedactionFailure:
            raise HTTPException(422, "Document redaction refused") from None
        return Response(result, media_type="application/json")
