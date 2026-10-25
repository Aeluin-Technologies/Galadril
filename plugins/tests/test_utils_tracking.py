"""Opening signals remain opt-in, bounded, encrypted and account-authorized."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from galadril_plugins.utils.mime import compose_reply
from galadril_plugins.utils.models import (
    ConnectionInput,
    Credentials,
    DraftInput,
    Principal,
)
from galadril_plugins.utils.store import MailError, MailStore
from galadril_plugins.utils.tracking import OpenTracking
from pydantic import SecretStr


class OpeningTrackingTest(unittest.TestCase):
    def test_private_capture_and_human_history_have_distinct_authority(
        self,
    ) -> None:
        from galadril_plugins.utils.service import MailService, create_app
        from galadril_plugins.utils.transport import MailTransport

        async def run() -> None:
            service = MailService(
                self.store,
                MailTransport(()),
                "k" * 40,
                "http://scribe",
                {},
                tracking_origin="https://mail.example.org",
                tracking_key="t" * 40,
            )
            draft = self.draft()
            self.store.approve(self.owner, self.account["id"], draft["id"], 1)
            self.store.claim_send(
                self.owner, self.account["id"], draft["id"], 1
            )
            url = service.tracking.prepare(
                self.owner, self.account["id"], draft["id"]
            )
            token = url.rsplit("/", 1)[-1].removesuffix(".gif")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(service)),
                base_url="http://email",
            ) as client:
                signal = {"token": token, "ip": "203.0.113.7"}
                self.assertEqual(
                    (
                        await client.post("/tracking/open", json=signal)
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    (
                        await client.post(
                            "/tracking/open",
                            json=signal,
                            headers={"x-email-tracking-key": "t" * 40},
                        )
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    (
                        await client.post(
                            "/tracking/open",
                            json=signal,
                            headers={"x-utils-key": "k" * 40},
                        )
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    (
                        await client.post(
                            "/operation",
                            json={"operation": "tracking_status"},
                            headers={"x-utils-key": "t" * 40},
                        )
                    ).status_code,
                    403,
                )
                headers = {
                    "x-utils-key": "k" * 40,
                    "x-utils-tenant": self.owner.tenant,
                    "x-utils-user": self.owner.user,
                }
                operation = {
                    "operation": "opens",
                    "account": self.account["id"],
                    "draft": draft["id"],
                }
                result = await client.post(
                    "/operation", json=operation, headers=headers
                )
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["items"][0]["ip"], "203.0.113.7")
                self.assertEqual(
                    (
                        await client.post(
                            "/operation",
                            json=operation,
                            headers={**headers, "x-utils-delegated": "true"},
                        )
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    (
                        await client.post(
                            "/operation",
                            json=operation,
                            headers={**headers, "x-utils-user": "other"},
                        )
                    ).status_code,
                    403,
                )
                result = await client.post(
                    "/operation",
                    json={
                        "operation": "save_draft",
                        "account": self.account["id"],
                        "content": {
                            "track_opens": True,
                            "to": ["recipient@example.org"],
                            "subject": "Draft",
                            "body": "Hello",
                        },
                    },
                    headers={**headers, "x-utils-delegated": "true"},
                )
                self.assertEqual(result.status_code, 403)

        asyncio.run(run())

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = MailStore(
            Path(self.directory.name) / "mail.db", {"one": b"k" * 32}, "one"
        )
        self.owner = Principal(tenant="tenant", user="owner")
        self.other = Principal(tenant="tenant", user="other")
        self.account = self.store.create(
            self.owner,
            ConnectionInput(
                name="Mailbox",
                address="owner@example.org",
                imap_host="imap.example.org",
                smtp_host="smtp.example.org",
            ),
            Credentials(username="owner", password=SecretStr("private")),
        )
        self.tracking = OpenTracking(self.store, "https://mail.example.org")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def draft(self, tracked: bool = True) -> dict[str, object]:
        return self.store.save_draft(
            self.owner,
            self.account["id"],
            None,
            None,
            DraftInput(
                to=("recipient@example.org",),
                subject="Review",
                body="Hello",
                track_opens=tracked,
            ),
        )

    def test_pixel_requires_opt_in_and_does_not_expose_mail_identities(
        self,
    ) -> None:
        draft = self.draft(False)
        self.assertIsNone(
            self.tracking.prepare(self.owner, self.account["id"], draft["id"])
        )
        draft = self.draft()
        self.assertIsNone(
            self.tracking.prepare(self.owner, self.account["id"], draft["id"])
        )
        self.store.approve(self.owner, self.account["id"], draft["id"], 1)
        self.store.claim_send(self.owner, self.account["id"], draft["id"], 1)
        url = self.tracking.prepare(self.owner, self.account["id"], draft["id"])
        self.assertRegex(
            url, r"^https://mail.example.org/email/open/[a-f0-9]{64}\.gif$"
        )
        content = DraftInput.model_validate(draft["content"])
        raw = compose_reply(
            "owner@example.org",
            content,
            "<delivery@example.org>",
            tracking_url=url,
        )
        self.assertIn(b"Content-Type: multipart/alternative", raw)
        self.assertNotIn(
            b"tracking",
            compose_reply(
                "owner@example.org", content, "<delivery@example.org>"
            ),
        )
        for identity in (
            self.owner.user,
            self.owner.tenant,
            self.account["id"],
            draft["id"],
            "recipient",
        ):
            self.assertNotIn(identity, url)

    def test_each_open_has_a_timestamp_and_encrypted_ip_with_live_read_authorization(
        self,
    ) -> None:
        draft = self.draft()
        self.store.approve(self.owner, self.account["id"], draft["id"], 1)
        self.store.claim_send(self.owner, self.account["id"], draft["id"], 1)
        url = self.tracking.prepare(self.owner, self.account["id"], draft["id"])
        token = url.rsplit("/", 1)[-1].removesuffix(".gif")
        now = int(time.time())
        self.tracking.record(token, "203.0.113.7", now=now)
        self.tracking.record(token, "2001:db8::2", now=now + 10)
        opens = self.tracking.history(
            self.owner, self.account["id"], draft["id"]
        )
        self.assertEqual(
            [item["ip"] for item in opens["items"]],
            ["2001:db8::2", "203.0.113.7"],
        )
        self.assertEqual(opens["items"][0]["opened_at"], now + 10)
        with self.assertRaises(MailError):
            self.tracking.history(self.other, self.account["id"], draft["id"])
        with self.store.database() as db:
            payload = str(
                [
                    tuple(row)
                    for row in db.execute("SELECT * FROM email_open_events")
                ]
            )
        self.assertNotIn("203.0.113.7", payload)
        self.assertNotIn("2001:db8::2", payload)
        self.assertNotIn(token, payload)

    def test_invalid_expired_and_excessive_opens_are_ignored(self) -> None:
        self.tracking.record("unknown", "invalid")
        draft = self.draft()
        self.store.approve(self.owner, self.account["id"], draft["id"], 1)
        self.store.claim_send(self.owner, self.account["id"], draft["id"], 1)
        url = self.tracking.prepare(self.owner, self.account["id"], draft["id"])
        token = url.rsplit("/", 1)[-1].removesuffix(".gif")
        self.tracking.record(token, "not-an-ip")
        self.tracking.record(token, "203.0.113.7", now=9_999_999_999)
        self.assertEqual(
            self.tracking.history(self.owner, self.account["id"], draft["id"])[
                "items"
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
