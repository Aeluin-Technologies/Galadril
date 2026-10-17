"""Download authorized storage without exposing signed URLs to inference providers."""

import asyncio
from collections.abc import Sequence
from typing import Literal

import httpx
from pydantic_ai._ssrf import build_url_with_ip, validate_and_resolve_url
from pydantic_ai.messages import (
    AudioUrl,
    BinaryContent,
    DocumentUrl,
    ImageUrl,
    UserContent,
)

from galadril_scribe.contracts import Attachment


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


class StorageMedia:
    __slots__ = ("client", "remaining", "cache")

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.remaining = 50 * 1024 * 1024
        self.cache: dict[str, BinaryContent] = {}

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
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity")
                    != "identity"
                ):
                    raise MediaFailure("Storage read failed")
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
                result.append(await self.download(attachment))
            else:
                result.extend(content("", [attachment])[1:])
        return result
