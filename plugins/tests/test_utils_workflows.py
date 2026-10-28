"""Durable mail approvals and parsing must survive retries without escalation."""

import tempfile
import unittest
from pathlib import Path

from galadril_plugins.utils.mime import compose_reply, parse_message
from galadril_plugins.utils.models import (
    ConnectionInput,
    Credentials,
    DraftInput,
    Principal,
)
from galadril_plugins.utils.store import MailError, MailStore
from pydantic import SecretStr


class MailWorkflows(unittest.TestCase):
    def test_imap_sync_does_not_clear_a_known_smtp_authentication_failure(
        self,
    ) -> None:
        self.store.sending_status(
            self.actor, self.account, "authentication_failed"
        )
        self.store.sync_status(self.actor, self.account, "ok")
        account = self.store.accounts(self.actor)[0]
        self.assertEqual(account["sync_status"], "ok")
        self.assertEqual(account["sending_status"], "authentication_failed")

    def test_pipeline_execution_requires_an_ingested_message_and_exact_authorized_revision(
        self,
    ) -> None:
        from galadril_plugins.utils.models import (
            PipelineBinding,
            ResponseProfile,
        )

        message = parse_message(
            b"From: alice@example.org\r\nTo: me@example.org\r\nMessage-ID: <one@example.org>\r\n\r\nHello"
        )
        key = self.store.record_message(
            self.actor, self.account, "INBOX", 1, 1, message
        )
        self.store.save_profile(
            self.actor,
            self.account,
            ResponseProfile(
                model="local",
                pipeline=PipelineBinding(
                    pipeline_id="mail", revision_id="a" * 32
                ),
            ),
        )
        with self.assertRaises(MailError):
            self.store.pipeline_actor(
                self.actor.tenant, self.account, key, "mail", "a" * 32
            )
        self.store.mark_ingested(self.actor, self.account, key)
        self.assertEqual(
            self.store.pipeline_actor(
                self.actor.tenant, self.account, key, "mail", "a" * 32
            ),
            self.actor,
        )
        for tenant, revision in [
            ("other", "a" * 32),
            (self.actor.tenant, "b" * 32),
        ]:
            with self.assertRaises(MailError):
                self.store.pipeline_actor(
                    tenant, self.account, key, "mail", revision
                )

    def test_sending_only_accounts_are_not_polled_and_can_send_approved_drafts(
        self,
    ) -> None:
        account = self.store.create(
            self.actor,
            ConnectionInput(
                name="Relay",
                address="me@example.org",
                smtp_host="mail-eu.smtp2go.com",
                smtp_port=2525,
            ),
            Credentials(username="smtp-user", password=SecretStr("private")),
        )
        self.assertNotIn(
            account["id"],
            [identifier for _, identifier, _ in self.store.workers()],
        )
        draft = self.store.save_draft(
            self.actor,
            account["id"],
            None,
            None,
            DraftInput(to=("to@example.org",), subject="Test", body="Body"),
        )
        self.store.approve(self.actor, account["id"], draft["id"], 1)
        claimed = self.store.claim_send(
            self.actor, account["id"], draft["id"], 1
        )
        self.assertEqual(claimed.to, ("to@example.org",))

    def test_disabled_connection_cannot_claim_an_approved_delivery(
        self,
    ) -> None:
        config, _ = self.store.connection(self.actor, self.account, "edit")
        self.store.update(
            self.actor,
            self.account,
            1,
            config.model_copy(update={"enabled": False}),
            None,
        )
        draft = self.store.save_draft(
            self.actor,
            self.account,
            None,
            None,
            DraftInput(to=("alice@example.org",), subject="Hello", body="Body"),
        )
        self.store.approve(self.actor, self.account, draft["id"], 1)
        with self.assertRaises(MailError):
            self.store.claim_send(self.actor, self.account, draft["id"], 1)

    def test_multipart_alternative_prefers_plain_text_and_html_is_inert(
        self,
    ) -> None:
        raw = b'From: alice@example.org\r\nTo: me@example.org\r\nContent-Type: multipart/alternative; boundary="x"\r\n\r\n--x\r\nContent-Type: text/html\r\n\r\n<p>HTML copy</p><script>hidden instructions</script>\r\n--x\r\nContent-Type: text/plain\r\n\r\nPlain copy\r\n--x--\r\n'
        self.assertEqual(parse_message(raw).body.strip(), "Plain copy")
        html = b"From: alice@example.org\r\nContent-Type: text/html\r\n\r\n<p>Hello</p><script>hidden instructions</script>"
        self.assertEqual(parse_message(html).body.strip(), "Hello")

    def test_lists_bound_private_payloads_but_explicit_reads_preserve_content(
        self,
    ) -> None:
        from galadril_plugins.utils.models import Attachment

        message = parse_message(
            b"From: alice@example.org\r\nTo: me@example.org\r\nMessage-ID: <large@example.org>\r\n\r\nHello"
        ).model_copy(
            update={
                "body": "x" * 20000,
                "attachments": (
                    Attachment(
                        name="file.txt",
                        content_type="text/plain",
                        content="eHl6",
                    ),
                ),
            }
        )
        identifier = self.store.record_message(
            self.actor, self.account, "INBOX", 7, 1, message
        )
        listing = self.store.messages(self.actor, self.account)
        self.assertLessEqual(len(listing[0]["message"]["body"]), 4096)
        self.assertEqual(listing[0]["message"]["attachments"][0]["content"], "")
        self.assertEqual(
            self.store.message(self.actor, self.account, identifier)
            .attachments[0]
            .content,
            "eHl6",
        )
        value = DraftInput(
            to=("alice@example.org",),
            subject="Files",
            body="y" * 20000,
            attachments=message.attachments,
        )
        draft = self.store.save_draft(
            self.actor, self.account, None, None, value
        )
        self.assertEqual(
            self.store.drafts(self.actor, self.account)[0]["content"][
                "attachments"
            ][0]["content"],
            "",
        )
        self.assertEqual(
            self.store.draft(self.actor, self.account, draft["id"])["content"][
                "body"
            ],
            value.body,
        )

    def test_automatic_replies_respect_the_configured_sender_limit(
        self,
    ) -> None:
        from galadril_plugins.utils.models import ResponseProfile

        config, _ = self.store.connection(self.actor, self.account, "use")
        self.store.save_profile(
            self.actor,
            self.account,
            ResponseProfile(
                model="local",
                mode="automatic",
                allowed_senders=("alice@example.org",),
                sender_daily_limit=1,
            ),
        )
        message = parse_message(
            b"From: alice@example.org\r\nTo: me@example.org\r\nMessage-ID: <one@example.org>\r\n\r\nHi"
        )
        first = self.store.save_draft(
            self.actor,
            self.account,
            None,
            None,
            DraftInput(
                to=("alice@example.org",),
                subject="Re",
                body="Reply",
                reply_to_message_id=message.message_id,
            ),
            source_id="source-one",
            automatic=True,
        )
        self.store.approve_automatic(
            self.actor, self.account, first["id"], message
        )
        other = message.model_copy(update={"message_id": "<two@example.org>"})
        second = self.store.save_draft(
            self.actor,
            self.account,
            None,
            None,
            DraftInput(
                to=("alice@example.org",),
                subject="Re",
                body="Reply",
                reply_to_message_id=other.message_id,
            ),
            source_id="source-two",
            automatic=True,
        )
        with self.assertRaises(MailError):
            self.store.approve_automatic(
                self.actor, self.account, second["id"], other
            )

    def test_distinct_messages_from_same_sender_are_allowed_without_a_sender_limit(
        self,
    ) -> None:
        from galadril_plugins.utils.models import ResponseProfile

        self.store.save_profile(
            self.actor,
            self.account,
            ResponseProfile(
                model="local",
                mode="automatic",
                allowed_senders=("alice@example.org",),
                sender_daily_limit=None,
                one_reply_per_thread=False,
            ),
        )
        for index in range(2):
            message = parse_message(
                f"From: alice@example.org\r\nTo: me@example.org\r\nMessage-ID: <{index}@example.org>\r\n\r\nA different question".encode()
            )
            draft = self.store.save_draft(
                self.actor,
                self.account,
                None,
                None,
                DraftInput(
                    to=("alice@example.org",),
                    subject="Re",
                    body="Reply",
                    reply_to_message_id=message.message_id,
                ),
                source_id=str(index),
                automatic=True,
            )
            self.store.approve_automatic(
                self.actor, self.account, draft["id"], message
            )

    def test_one_acknowledgment_per_thread_uses_rfc_thread_identity(
        self,
    ) -> None:
        from galadril_plugins.utils.models import ResponseProfile

        self.store.save_profile(
            self.actor,
            self.account,
            ResponseProfile(
                model="local",
                mode="automatic",
                allowed_senders=("alice@example.org",),
                sender_daily_limit=None,
                one_reply_per_thread=True,
            ),
        )
        for index in range(2):
            message = parse_message(
                f"From: alice@example.org\r\nTo: me@example.org\r\nMessage-ID: <{index}@example.org>\r\nReferences: <root@example.org>\r\n\r\nFollow-up".encode()
            )
            self.assertEqual(message.thread_id, "<root@example.org>")
            draft = self.store.save_draft(
                self.actor,
                self.account,
                None,
                None,
                DraftInput(
                    to=("alice@example.org",),
                    subject="Re",
                    body="Acknowledged",
                    reply_to_message_id=message.message_id,
                ),
                source_id=str(index),
                automatic=True,
            )
            if index == 0:
                self.store.approve_automatic(
                    self.actor, self.account, draft["id"], message
                )
            else:
                with self.assertRaises(MailError):
                    self.store.approve_automatic(
                        self.actor, self.account, draft["id"], message
                    )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = MailStore(
            Path(self.directory.name) / "mail.db", {"v1": b"x" * 32}, "v1"
        )
        self.actor = Principal(tenant="t", user="owner")
        self.account = self.store.create(
            self.actor,
            ConnectionInput(
                name="Mailbox",
                address="me@example.org",
                imap_host="imap.example.org",
                smtp_host="smtp.example.org",
            ),
            Credentials(username="me", password=SecretStr("pw")),
        )["id"]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_approval_is_invalidated_by_any_edit(self) -> None:
        value = DraftInput(
            to=("alice@example.org",), subject="Response", body="Approved words"
        )
        draft = self.store.save_draft(
            self.actor, self.account, None, None, value
        )
        self.store.approve(self.actor, self.account, draft["id"], 1)
        changed = self.store.save_draft(
            self.actor,
            self.account,
            draft["id"],
            1,
            value.model_copy(update={"body": "Different words"}),
        )
        self.assertEqual(changed["revision"], 2)
        with self.assertRaises(MailError):
            self.store.claim_send(self.actor, self.account, draft["id"], 2)

    def test_send_claim_is_atomic_and_uncertain_outcome_is_never_retried(
        self,
    ) -> None:
        draft = self.store.save_draft(
            self.actor,
            self.account,
            None,
            None,
            DraftInput(
                to=("alice@example.org",), subject="Response", body="Once"
            ),
        )
        self.store.approve(self.actor, self.account, draft["id"], 1)
        first = self.store.claim_send(self.actor, self.account, draft["id"], 1)
        self.assertEqual(first.body, "Once")
        with self.assertRaises(MailError):
            self.store.claim_send(self.actor, self.account, draft["id"], 1)
        self.store.finish_send(self.actor, self.account, draft["id"], False)
        with self.assertRaises(MailError):
            self.store.claim_send(self.actor, self.account, draft["id"], 1)
        reopened = MailStore(self.store.path, {"v1": b"x" * 32}, "v1")
        self.assertEqual(
            reopened.drafts(self.actor, self.account)[0]["status"], "uncertain"
        )

    def test_checkpoint_message_identity_and_tenant_are_durable(self) -> None:
        message = parse_message(
            b"From: Alice <alice@example.org>\r\nTo: me@example.org\r\nMessage-ID: <one@example.org>\r\nSubject: Need help\r\n\r\nHello"
        )
        self.store.record_message(
            self.actor, self.account, "INBOX", 7, 42, message
        )
        self.store.record_message(
            self.actor, self.account, "INBOX", 7, 42, message
        )
        self.assertEqual(len(self.store.messages(self.actor, self.account)), 1)
        self.assertEqual(
            self.store.cursor(self.actor, self.account, "INBOX"), (7, 42)
        )
        self.assertEqual(
            self.store.cursor(self.actor, self.account, "Archive"), (None, 0)
        )
        with self.assertRaises(MailError):
            self.store.messages(
                Principal(tenant="other", user="owner"), self.account
            )

    def test_mime_keeps_threading_attachments_and_auto_response_safety(
        self,
    ) -> None:
        raw = b"From: alice@example.org\r\nTo: me@example.org\r\nMessage-ID: <one@example.org>\r\nAuto-Submitted: auto-generated\r\nSubject: Status\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nStatus report"
        message = parse_message(raw)
        self.assertFalse(message.reply_allowed)
        draft = DraftInput(
            to=("alice@example.org",),
            subject="Re: Status",
            body="Acknowledged",
            reply_to_message_id="<one@example.org>",
        )
        output = compose_reply(
            "me@example.org", draft, "<delivery@example.org>", automatic=True
        )
        self.assertIn(b"Auto-Submitted: auto-replied", output)
        self.assertIn(b"In-Reply-To: <one@example.org>", output)
        self.assertIn(b"Message-ID: <delivery@example.org>", output)

    def test_headers_reject_injection(self) -> None:
        with self.assertRaises(ValueError):
            DraftInput(
                to=("alice@example.org\r\nBcc:evil@example.org",),
                subject="x",
                body="x",
            )
        with self.assertRaises(ValueError):
            DraftInput(
                to=("alice@example.org",),
                subject="x\r\nBcc:evil@example.org",
                body="x",
            )

    def test_changing_sending_identity_invalidates_existing_approval(
        self,
    ) -> None:
        draft = self.store.save_draft(
            self.actor,
            self.account,
            None,
            None,
            DraftInput(
                to=("alice@example.org",), subject="Response", body="Once"
            ),
        )
        self.store.approve(self.actor, self.account, draft["id"], 1)
        config, credentials = self.store.connection(
            self.actor, self.account, "edit"
        )
        self.store.update(
            self.actor,
            self.account,
            1,
            config.model_copy(update={"address": "other@example.org"}),
            credentials,
        )
        with self.assertRaises(MailError):
            self.store.claim_send(self.actor, self.account, draft["id"], 1)


if __name__ == "__main__":
    unittest.main()
