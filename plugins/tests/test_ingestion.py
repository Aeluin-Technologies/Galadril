"""Input services must use the authorized upload protocol without leaking credentials."""

import asyncio

import httpx
import pytest
from galadril_plugins.ingestion import (
    IngestionClient,
    IngestionError,
    IngestionSettings,
)


def test_ingestion_stages_without_credentials_and_finalizes_before_receipt() -> (
    None
):
    calls: list[str] = []

    async def token() -> str:
        return "private-token"

    async def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage":
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            assert request.content == b"document"
            calls.append("put")
            return httpx.Response(200)
        assert request.headers["authorization"] == "Bearer private-token"
        if b"requestStagingUpload" in request.content:
            calls.append("stage")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "requestStagingUpload": {
                            "stagingKey": "tenant/user/email/" + "a" * 32,
                            "uploadUrl": "https://storage/staging?signature=private",
                        }
                    }
                },
            )
        calls.append("complete")
        assert b'"group":"email"' in request.content
        return httpx.Response(
            200,
            json={"data": {"completeUpload": "tenant/raw/email/message.txt"}},
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport),
            headers={
                "authorization": "default-secret",
                "cookie": "private-cookie",
            },
        ) as http:
            client = IngestionClient(
                IngestionSettings(
                    gateway_url="https://gateway/graphql",
                    storage_origins=("https://storage",),
                    tenant_id="tenant",
                    subject="user",
                    group_id="email",
                ),
                http,
                token,
            )
            receipt = await client.upload(
                b"document", name="message.txt", content_type="text/plain"
            )
            assert receipt.key == "tenant/raw/email/message.txt"

    asyncio.run(run())
    assert calls == ["stage", "put", "complete"]


@pytest.mark.parametrize(
    "failure",
    ["denied", "scope", "origin", "redirect", "finalize", "oversized"],
)
def test_ingestion_fails_closed_without_receipts_or_private_errors(
    failure: str,
) -> None:
    calls: list[str] = []

    async def token() -> str:
        return "private-token"

    async def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        if request.url.host == "storage":
            return httpx.Response(
                307 if failure == "redirect" else 200,
                headers={"location": "https://evil/private"},
            )
        if b"requestStagingUpload" in request.content:
            if failure == "denied":
                return httpx.Response(
                    200,
                    json={"errors": [{"message": "private-provider-details"}]},
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "requestStagingUpload": {
                            "stagingKey": (
                                "other" if failure == "scope" else "tenant"
                            )
                            + "/user/email/"
                            + "a" * 32,
                            "uploadUrl": "https://"
                            + ("evil" if failure == "origin" else "storage")
                            + "/staging?secret=private",
                        }
                    }
                },
            )
        return httpx.Response(
            200, json={"errors": [{"message": "private-provider-details"}]}
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as http:
            client = IngestionClient(
                IngestionSettings(
                    gateway_url="https://gateway/graphql",
                    storage_origins=("https://storage",),
                    tenant_id="tenant",
                    subject="user",
                    group_id="email",
                    max_bytes=8,
                ),
                http,
                token,
            )
            with pytest.raises(IngestionError) as error:
                await client.upload(
                    b"too long document"
                    if failure == "oversized"
                    else b"document",
                    name="message.txt",
                    content_type="text/plain",
                )
            assert "private" not in str(error.value)

    asyncio.run(run())
    if failure in {"denied", "scope", "origin"}:
        assert calls == ["gateway"]
    if failure == "oversized":
        assert not calls


def test_ingestion_has_a_total_deadline_even_with_a_waiting_transport() -> None:
    async def token() -> str:
        return "token"

    async def wait(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(wait), timeout=None
        ) as http:
            settings = IngestionSettings(
                gateway_url="https://gateway/graphql",
                storage_origins=("https://storage",),
                tenant_id="tenant",
                subject="user",
                group_id="email",
                timeout_seconds=0.01,
            )
            with pytest.raises(IngestionError):
                await IngestionClient(settings, http, token).upload(
                    b"data", name="message.txt", content_type="text/plain"
                )

    asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
