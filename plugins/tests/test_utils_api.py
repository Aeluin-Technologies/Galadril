"""The production service binds identity to authenticated Gateway headers."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from galadril_plugins.ingestion import IngestionSettings
from galadril_plugins.utils.mime import parse_message
from galadril_plugins.utils.models import (
    Attachment,
    ConnectionInput,
    Credentials,
    Principal,
    ResponseProfile,
)
from galadril_plugins.utils.service import MailService, create_app
from galadril_plugins.utils.store import MailError, MailStore
from galadril_plugins.utils.transport import MailTransport
from pydantic import SecretStr


class ServiceSecurity(unittest.TestCase):
    def test_discovery_is_authenticated_credential_free_and_not_delegated(
        self,
    ) -> None:
        from unittest.mock import AsyncMock, patch

        from galadril_plugins.utils.discovery import DNSResolver

        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                service = MailService(
                    MailStore(
                        Path(directory) / "mail.db", {"one": b"x" * 32}, "one"
                    ),
                    MailTransport(),
                    "k" * 48,
                    "http://scribe",
                    {},
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=create_app(service)),
                    base_url="http://utils",
                ) as client:
                    headers = {
                        "x-utils-key": "k" * 48,
                        "x-utils-tenant": "t",
                        "x-utils-user": "owner",
                    }
                    body = {"operation": "discover", "domain": "example.org"}
                    self.assertEqual(
                        (
                            await client.post("/operation", json=body)
                        ).status_code,
                        403,
                    )
                    with patch.object(
                        DNSResolver,
                        "services",
                        new_callable=AsyncMock,
                        return_value=(),
                    ) as query:
                        response = await client.post(
                            "/operation", headers=headers, json=body
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(
                            response.json()["imap_status"], "absent"
                        )
                        self.assertEqual(
                            response.headers["cache-control"], "no-store"
                        )
                        self.assertEqual(query.await_count, 3)
                    self.assertEqual(
                        (
                            await client.post(
                                "/operation",
                                headers={
                                    **headers,
                                    "x-utils-delegated": "true",
                                },
                                json=body,
                            )
                        ).status_code,
                        403,
                    )
                    for extra in (
                        {
                            "credentials": {
                                "username": "private",
                                "password": "private",
                            }
                        },
                        {"domain": "http://127.0.0.1"},
                        {"domain": ""},
                    ):
                        response = await client.post(
                            "/operation",
                            headers=headers,
                            json={**body, **extra},
                        )
                        self.assertEqual(response.status_code, 422)

        asyncio.run(run())

    def test_use_permission_can_generate_without_connection_edit_permission(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                store = MailStore(
                    Path(directory) / "mail.db", {"one": b"x" * 32}, "one"
                )
                owner = Principal(tenant="t", user="owner")
                editor = Principal(tenant="t", user="drafter")
                identifier = store.create(
                    owner,
                    ConnectionInput(
                        name="Mail",
                        address="me@example.org",
                        imap_host="imap.example.org",
                        smtp_host="smtp.example.org",
                    ),
                    Credentials(username="login", password=SecretStr("secret")),
                )["id"]
                store.save_profile(
                    owner, identifier, ResponseProfile(model="local")
                )
                store.grant(owner, identifier, editor.user, ("use", "read"))
                message = store.record_message(
                    owner,
                    identifier,
                    "INBOX",
                    1,
                    1,
                    parse_message(
                        b"From: alice@example.org\r\nTo: me@example.org\r\nMessage-ID: <one@example.org>\r\nSubject: Help\r\n\r\nQuestion"
                    ),
                )
                service = MailService(
                    store, MailTransport(), "x" * 48, "http://scribe", {}
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            200,
                            json={
                                "body": "Reply",
                                "category": "support",
                                "summary": "Question",
                                "model": "local",
                            },
                        )
                    )
                ) as client:
                    service._http = client
                    draft = await service.generate(editor, identifier, message)
                self.assertEqual(draft["content"]["body"], "Reply")
                self.assertEqual(
                    store.messages(owner, identifier)[0]["label"], "support"
                )

        asyncio.run(run())

    def test_account_lock_identity_survives_large_account_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = MailService(
                MailStore(
                    Path(directory) / "mail.db", {"one": b"x" * 32}, "one"
                ),
                MailTransport(),
                "x" * 48,
                "http://scribe",
                {},
            )
            actor = Principal(tenant="t", user="owner")
            first = service.lock(actor, "a" * 32)
            for index in range(2050):
                service.lock(actor, f"{index:032x}")
            self.assertIs(service.lock(actor, "a" * 32), first)

    def test_draft_on_demand_does_not_generate_for_collected_mail(self) -> None:
        from unittest.mock import MagicMock

        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                store = MailStore(
                    Path(directory) / "mail.db", {"one": b"x" * 32}, "one"
                )
                actor = Principal(tenant="t", user="owner")
                config = ConnectionInput(
                    name="Mail",
                    address="me@example.org",
                    imap_host="imap.example.org",
                    smtp_host="smtp.example.org",
                )
                identifier = store.create(
                    actor,
                    config,
                    Credentials(username="login", password=SecretStr("secret")),
                )["id"]
                store.save_profile(
                    actor,
                    identifier,
                    ResponseProfile(model="local", mode="draft"),
                )
                transport = MagicMock(spec=MailTransport)

                def collect(config, credentials, cursor, consume, advance):
                    consume(
                        "INBOX",
                        1,
                        1,
                        parse_message(
                            b"From: alice@example.org\r\nMessage-ID: <one@example.org>\r\n\r\nQuestion"
                        ),
                    )
                    return 1

                transport.sync.side_effect = collect
                service = MailService(
                    store, transport, "x" * 48, "http://scribe", {}
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(503)
                    )
                ) as client:
                    service._http = client
                    self.assertEqual(await service.sync(actor, identifier), 1)
                self.assertEqual(store.drafts(actor, identifier), [])

        asyncio.run(run())

    def test_ingestion_commits_attachments_before_the_full_structured_email(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                store = MailStore(
                    Path(directory) / "mail.db", {"one": b"x" * 32}, "one"
                )
                actor = Principal(tenant="t", user="owner")
                config = ConnectionInput(
                    name="Mail",
                    address="me@example.org",
                    imap_host="imap.example.org",
                    smtp_host="smtp.example.org",
                    ingest=True,
                )
                identifier = store.create(
                    actor,
                    config,
                    Credentials(username="login", password=SecretStr("secret")),
                )["id"]
                message = parse_message(
                    b"From: alice@example.org\r\nTo: me@example.org\r\nSubject: Work\r\n\r\nText"
                ).model_copy(
                    update={
                        "body": "x" * 5000,
                        "attachments": (
                            Attachment(
                                name="note.txt",
                                content_type="text/plain",
                                content="aGVsbG8=",
                            ),
                        ),
                    }
                )
                key = store.record_message(
                    actor, identifier, "INBOX", 7, 1, message
                )
                token = Path(directory) / "token"
                token.write_text("ingestion-token")
                settings = IngestionSettings(
                    gateway_url="http://gateway/graphql",
                    storage_origins=("http://storage",),
                    tenant_id="t",
                    subject="service",
                    group_id="email",
                )
                service = MailService(
                    store,
                    MailTransport(),
                    "x" * 48,
                    "http://scribe",
                    {"t": (settings, token)},
                )
                committed = []
                contents = []

                def gateway(request: httpx.Request) -> httpx.Response:
                    if request.method == "PUT":
                        contents.append(request.content)
                        return httpx.Response(200)
                    body = json.loads(request.content)
                    if "requestStagingUpload" in body["query"]:
                        return httpx.Response(
                            200,
                            json={
                                "data": {
                                    "requestStagingUpload": {
                                        "stagingKey": "t/service/email/"
                                        + "a" * 32,
                                        "uploadUrl": "http://storage/staging",
                                    }
                                }
                            },
                        )
                    name = body["variables"]["name"]
                    committed.append(name)
                    return httpx.Response(
                        200,
                        json={
                            "data": {"completeUpload": "t/raw/email/" + name}
                        },
                    )

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(gateway)
                ) as client:
                    service._http = client
                    await service.ingest(actor, identifier, config)
                self.assertEqual(
                    committed,
                    [
                        f"email-{identifier}-{key}-attachment-0",
                        f"email-{identifier}-{key}.json",
                    ],
                )
                envelope = json.loads(contents[-1])
                self.assertEqual(len(envelope["content"]), 5000)
                self.assertEqual(envelope["metadata"]["subject"], "Work")
                self.assertEqual(
                    store.messages(actor, identifier, pending=True), []
                )

        asyncio.run(run())

    def test_disabled_background_owner_is_rejected_before_provider_access(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                service = MailService(
                    MailStore(
                        Path(directory) / "mail.db", {"one": b"s" * 32}, "one"
                    ),
                    MailTransport(),
                    "k" * 48,
                    "http://scribe",
                    {},
                )
                service.gateway = "http://gateway"
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            403, json={"detail": "disabled"}
                        )
                    )
                ) as client:
                    service._http = client
                    with self.assertRaises(MailError):
                        await service.authorize(
                            Principal(tenant="t", user="disabled")
                        )

        asyncio.run(run())

    def test_request_cannot_impersonate_owner_or_reveal_through_list(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                service = MailService(
                    MailStore(
                        Path(directory) / "mail.db", {"one": b"s" * 32}, "one"
                    ),
                    MailTransport(),
                    "k" * 48,
                    "http://scribe",
                    {},
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=create_app(service)),
                    base_url="http://utils",
                ) as api:
                    body = {"operation": "accounts"}
                    self.assertEqual(
                        (await api.post("/operation", json=body)).status_code,
                        403,
                    )
                    headers = {
                        "x-utils-key": "k" * 48,
                        "x-utils-tenant": "t",
                        "x-utils-user": "owner",
                    }
                    created = await api.post(
                        "/operation",
                        headers=headers,
                        json={
                            "operation": "create",
                            "config": {
                                "name": "Work",
                                "address": "me@example.org",
                                "imap_host": "imap.example.org",
                                "smtp_host": "smtp.example.org",
                            },
                            "credentials": {
                                "username": "private-user",
                                "password": "private-value",
                            },
                        },
                    )
                    self.assertEqual(created.status_code, 200)
                    account = created.json()["id"]
                    listing = await api.post(
                        "/operation", headers=headers, json=body
                    )
                    self.assertNotIn("private-user", listing.text)
                    self.assertNotIn("private-value", listing.text)
                    headers["x-utils-user"] = "pipeline-viewer"
                    self.assertEqual(
                        (
                            await api.post(
                                "/operation",
                                headers=headers,
                                json={
                                    "operation": "reveal",
                                    "account": account,
                                },
                            )
                        ).status_code,
                        403,
                    )
                    self.assertEqual(
                        (
                            await api.post(
                                "/operation",
                                headers=headers,
                                json={"operation": "accounts", "user": "owner"},
                            )
                        ).status_code,
                        422,
                    )
                    headers["x-utils-user"] = "owner"
                    result = await api.post(
                        "/operation",
                        headers=headers,
                        json={"operation": "reveal", "account": account},
                    )
                    self.assertEqual(result.json()["password"], "private-value")
                    self.assertIn("no-store", result.headers["cache-control"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
