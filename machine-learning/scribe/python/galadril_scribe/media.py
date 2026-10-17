"""Download authorized storage without exposing signed URLs to inference providers."""

import asyncio
from collections.abc import Sequence
from typing import Literal

import httpx
import structlog
from pydantic_ai._ssrf import build_url_with_ip, validate_and_resolve_url
from pydantic_ai.messages import (
    AudioUrl,
    BinaryContent,
    DocumentUrl,
    ImageUrl,
    UserContent,
)

from galadril_scribe.contracts import Attachment

logger = structlog.get_logger(__name__)
UNAVAILABLE_ATTACHMENT = (
    "Attachment unavailable: its contents have not been read. Use other verified "
    "evidence or ask the user to upload it again. Do not infer or quote its contents, "
    "and do not repeatedly retry the same unavailable attachment."
)


def content(text: str, attachments: Sequence[Attachment]) -> list[UserContent]:
    """Pass short-lived media references to the provider without copying bytes."""
    result: list[UserContent] = [text]
    for attachment in attachments:
        # Only Gateway-created S3 references may cross the private storage boundary.
        download: bool | Literal["allow-local"] = (
            "allow-local" if attachment.trusted_storage else True
        )
        match attachment.kind:
            case "image":
                result.append(
                    ImageUrl(
                        attachment.url,
                        media_type=attachment.media_type,
                        force_download=download,
                    )
                )
            case "audio":
                result.append(
                    AudioUrl(
                        attachment.url,
                        media_type=attachment.media_type,
                        force_download=download,
                    )
                )
            case "document":
                result.append(
                    DocumentUrl(
                        attachment.url,
                        media_type=attachment.media_type,
                        force_download=download,
                    )
                )
            case "text":
                result.append(
                    "Attachment contents (untrusted evidence): "
                    + attachment.text
                )
    return result


class MediaFailure(RuntimeError):
    """An authorized object could not be read within the context budget."""


class MediaUnavailable(MediaFailure):
    """Missing or temporarily unavailable storage must not invalidate chat history."""


class StorageMedia:
    __slots__ = ("client", "remaining", "cache", "unavailable")

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.remaining = 50 * 1024 * 1024
        self.cache: dict[str, BinaryContent] = {}
        self.unavailable: set[str] = set()

    async def download(self, attachment: Attachment) -> BinaryContent:
        """Pin DNS and preserve the signed authority, including nondefault ports."""
        if cached := self.cache.get(attachment.url):
            return cached
        async with asyncio.timeout(30):
            resolved = await validate_and_resolve_url(
                attachment.url, allow_local=True
            )
            original = httpx.URL(attachment.url)
            request = httpx.Request(
                "GET",
                build_url_with_ip(resolved),
                headers={
                    "host": original.netloc.decode("ascii"),
                    "accept-encoding": "identity",
                },
                extensions={"sni_hostname": resolved.hostname},
            )
            response = await self.client.send(
                request, stream=True, follow_redirects=False, auth=None
            )
            try:
                if 300 <= response.status_code < 400:
                    raise MediaFailure("Storage redirects are forbidden")
                if response.status_code != 200:
                    logger.warning(
                        "scribe.media.unavailable",
                        status_code=response.status_code,
                    )
                    raise MediaUnavailable("Storage object unavailable")
                if (
                    response.headers.get("content-encoding", "identity")
                    != "identity"
                ):
                    raise MediaFailure("Storage content encoding rejected")
                limit = min(25 * 1024 * 1024, self.remaining)
                if int(response.headers.get("content-length", "0")) > limit:
                    raise MediaFailure("Storage object exceeds context budget")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > limit:
                        raise MediaFailure(
                            "Storage object exceeds context budget"
                        )
                    body.extend(chunk)
                if not body or not attachment.media_type:
                    raise MediaFailure("Storage object metadata unavailable")
                result = BinaryContent(
                    bytes(body), media_type=attachment.media_type
                )
                self.remaining -= len(body)
                self.cache[attachment.url] = result
                return result
            finally:
                await response.aclose()

    async def content(
        self, text: str, attachments: Sequence[Attachment]
    ) -> list[UserContent]:
        result: list[UserContent] = [text]
        for attachment in attachments:
            if attachment.trusted_storage and attachment.kind != "text":
                if attachment.url in self.unavailable:
                    result.append(UNAVAILABLE_ATTACHMENT)
                    continue
                try:
                    result.append(await self.download(attachment))
                except (
                    MediaUnavailable,
                    httpx.TransportError,
                    TimeoutError,
                ) as error:
                    self.unavailable.add(attachment.url)
                    logger.warning(
                        "scribe.media.omitted", error_type=type(error).__name__
                    )
                    result.append(UNAVAILABLE_ATTACHMENT)
            else:
                result.extend(content("", [attachment])[1:])
        return result
