"""Project supplied documents before protected specialists receive them."""

import base64

import httpx

from galadril_scribe.contracts import Attachment, RunRequest
from galadril_scribe.media import StorageMedia
from galadril_scribe.redaction import (
    RedactionFailure,
    RedactionRuntime,
    private_model_directory,
)
from galadril_scribe.redaction_contracts import (
    MAX_DOCUMENT_BYTES,
    PrivateBatch,
    PrivateBatchItem,
)


class PrivateContextProcessor:
    __slots__ = ("model_dir", "runtime")

    def __init__(self, model_dir: str) -> None:
        self.model_dir = model_dir
        self.runtime = RedactionRuntime()

    def available(self) -> bool:
        try:
            private_model_directory(self.model_dir)
        except (OSError, ValueError):
            return False
        return True

    async def project_sources(self, sources: PrivateBatch) -> tuple[str, ...]:
        values = await self.runtime.run_private(sources, self.model_dir)
        if len(values) != len(sources):
            raise RedactionFailure("Private projection incomplete")
        return values

    async def project_document(
        self, mime: str, content: bytes, encoded: str
    ) -> str:
        if not self.available():
            raise ValueError("Required privacy preprocessing is unavailable")
        source: PrivateBatchItem
        if mime == "application/pdf" and len(content) <= MAX_DOCUMENT_BYTES:
            source = ("pdf", encoded)
        elif mime in {"text/plain", "text/csv"} and len(content) <= 65536:
            source = ("text", content.decode("utf-8-sig", errors="strict"))
        else:
            raise RedactionFailure("Unsupported private document")
        values = await self.project_sources((source,))
        if not values[0].strip():
            raise RedactionFailure("Private projection incomplete")
        return values[0]

    async def prepare(
        self, request: RunRequest, client: httpx.AsyncClient
    ) -> RunRequest:
        sources: list[PrivateBatchItem] = []
        media = StorageMedia(client)
        media.remaining = MAX_DOCUMENT_BYTES
        positions: dict[str, int] = {}

        async def attachment(value: Attachment) -> int:
            if value.kind == "text":
                sources.append(("text", value.text))
                return len(sources) - 1
            if (
                value.kind != "document"
                or value.media_type != "application/pdf"
                or not value.trusted_storage
            ):
                raise RedactionFailure("Unsupported private attachment")
            if value.url not in positions:
                document = await media.download(value)
                sources.append(
                    ("pdf", base64.b64encode(document.data).decode())
                )
                positions[value.url] = len(sources) - 1
            return positions[value.url]

        attached = [await attachment(value) for value in request.attachments]
        history = [
            [await attachment(value) for value in message.attachments]
            for message in request.history
        ]
        values = await self.project_sources(tuple(sources))

        def projected(
            indices: list[int], originals: list[Attachment]
        ) -> list[Attachment]:
            return [
                Attachment(
                    kind="text",
                    text=values[index].replace("\f", "\n"),
                    file_id=original.file_id,
                )
                for index, original in zip(indices, originals, strict=True)
            ]

        return request.model_copy(
            update={
                "attachments": projected(attached, request.attachments),
                "history": [
                    message.model_copy(
                        update={
                            "attachments": projected(
                                indices, message.attachments
                            )
                        }
                    )
                    for message, indices in zip(
                        request.history, history, strict=True
                    )
                ],
            }
        )
