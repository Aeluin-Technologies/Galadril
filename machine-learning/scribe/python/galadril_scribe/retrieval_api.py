"""Only workload-authorized Gateway requests reach document inference."""

import asyncio
from typing import TypeVar

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from galadril_scribe.retrieval_contracts import (
    EmbedTexts,
    ExtractFacts,
    ParseDocument,
    RankPassages,
    RecognizeFacts,
)
from galadril_scribe.retrieval_runtime import DocumentRuntime

T = TypeVar("T", bound=BaseModel)


def install_documents(app: FastAPI, runtime: DocumentRuntime) -> None:
    active = 0
    background = 0

    async def payload(
        incoming: Request, contract: type[T], limit: int = 2097152
    ) -> T:
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                async for part in incoming.stream():
                    if len(body) + len(part) > limit:
                        raise HTTPException(
                            413, "Document request exceeds limit"
                        )
                    body.extend(part)
            return contract.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Document request rejected") from None
        except TimeoutError:
            raise HTTPException(408, "Document request timed out") from None

    async def execute(operation: str, request: BaseModel) -> Response:
        nonlocal active, background
        interactive = (
            isinstance(request, (RankPassages, RecognizeFacts))
            or isinstance(request, EmbedTexts)
            and request.query
        )
        if active >= 4 or not interactive and background >= 2:
            raise HTTPException(429, "Document runtime capacity exceeded")
        active += 1
        if not interactive:
            background += 1
        try:
            async with asyncio.timeout(180):
                match request:
                    case ParseDocument():
                        result = await runtime.parse(request)
                    case EmbedTexts():
                        result = await runtime.embed(request)
                    case ExtractFacts():
                        result = await runtime.extract(request)
                    case RankPassages():
                        result = await runtime.rank(request)
                    case RecognizeFacts():
                        result = await runtime.recognize(request)
                    case _:
                        raise ValueError("Unknown document operation")
            return Response(
                result.model_dump_json(), media_type="application/json"
            )
        except Exception as error:
            structlog.get_logger().warning(
                "documents.operation.failed",
                operation=operation,
                error_type=type(error).__name__,
            )
            raise HTTPException(503, "Document operation unavailable") from None
        finally:
            active -= 1
            if not interactive:
                background -= 1

    @app.post("/documents/parse")
    async def parse(incoming: Request) -> Response:
        return await execute(
            "parse", await payload(incoming, ParseDocument, 35_000_000)
        )

    @app.post("/documents/embed")
    async def embed(incoming: Request) -> Response:
        return await execute(
            "embed", await payload(incoming, EmbedTexts, 36_000_000)
        )

    @app.post("/documents/extract")
    async def extract(incoming: Request) -> Response:
        return await execute("extract", await payload(incoming, ExtractFacts))

    @app.post("/documents/rank")
    async def rank(incoming: Request) -> Response:
        return await execute(
            "rank", await payload(incoming, RankPassages, 8388608)
        )

    @app.post("/documents/recognize")
    async def recognize(incoming: Request) -> Response:
        return await execute(
            "recognize", await payload(incoming, RecognizeFacts)
        )
