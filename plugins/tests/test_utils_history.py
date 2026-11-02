"""Mailbox history remains private and cannot acquire sending authority."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from galadril_plugins.utils.mime import parse_message
from galadril_plugins.utils.models import (
    ConnectionInput,
    Credentials,
    DraftInput,
    Principal,
)
from galadril_plugins.utils.service import MailService, Operation, create_app
from galadril_plugins.utils.store import MailError, MailStore
from galadril_plugins.utils.transport import MailTransport
from pydantic import SecretStr, ValidationError


class History(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MailStore(
            Path(self.directory.name) / "mail.db", {"one": b"h" * 32}, "one"
        )
        self.owner = Principal(tenant="one", user="owner")
        self.config = ConnectionInput(
            name="Mail",
            address="me@example.org",
            imap_host="imap.example.org",
            smtp_host="smtp.example.org",
        )
        self.account = str(
            self.store.create(
                self.owner,
                self.config,
                Credentials(username="me", password=SecretStr("private")),
            )["id"]
        )
        self.message = parse_message(
            b"From: sender@example.org\r\nTo: me@example.org\r\nMessage-ID: <source@example.org>\r\nSubject: Question\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nContent-Type: text/plain\r\n\r\nQuestion\r\n--x\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename=note.txt\r\n\r\nPrivate attachment\r\n--x--"
        )
        self.message_id = self.store.record_message(
            self.owner, self.account, "INBOX", 1, 1, self.message
        )

    def test_full_collection_does_not_enable_document_indexing(self) -> None:
        self.assertEqual(self.config.initial_sync, "all")
        self.assertFalse(self.config.ingest)

    def test_chat_cannot_approve_or_send_even_with_an_approved_draft_and_full_user_rights(
        self,
    ) -> None:
        async def run() -> None:
            transport = MagicMock(spec=MailTransport)
            service = MailService(
                self.store, transport, "k" * 48, "http://scribe", {}
            )
            draft = self.store.save_draft(
                self.owner,
                self.account,
                None,
                None,
                DraftInput(
                    to=("sender@example.org",),
                    subject="Reply",
                    body="For human review",
                ),
            )
            for operation in ("approve", "send"):
                with self.assertRaisesRegex(MailError, "FORBIDDEN"):
                    await service.operate(
                        self.owner,
                        Operation(
                            operation=operation,
                            account=self.account,
                            draft=draft["id"],
                            revision=draft["revision"],
                        ),
                        delegated=True,
                    )
            self.assertEqual(
                self.store.draft(self.owner, self.account, draft["id"])[
                    "status"
                ],
                "draft",
            )
            self.store.approve(
                self.owner, self.account, draft["id"], draft["revision"]
            )
            with self.assertRaisesRegex(MailError, "FORBIDDEN"):
                await service.operate(
                    self.owner,
                    Operation(
                        operation="send",
                        account=self.account,
                        draft=draft["id"],
                        revision=draft["revision"],
                    ),
                    delegated=True,
                )
            self.assertEqual(
                self.store.draft(self.owner, self.account, draft["id"])[
                    "status"
                ],
                "approved",
            )
            transport.send.assert_not_called()

        asyncio.run(run())

    def test_scribe_can_decline_reply_and_output_cannot_bypass_the_decision(
        self,
    ) -> None:
        from galadril_plugins.utils.models import DeployedMailPipeline
        from galadril_plugins.utils.service import (
            PipelineDecision,
            PipelineMail,
        )

        async def run() -> None:
            self.store.mark_ingested(self.owner, self.account, self.message_id)
            transport = MagicMock(spec=MailTransport)
            service = MailService(
                self.store, transport, "k" * 48, "http://scribe", {}
            )
            policy = DeployedMailPipeline.model_validate(
                {
                    "pipeline_id": "mail",
                    "revision_id": "a" * 32,
                    "policy": {
                        "sender_account_id": self.account,
                        "mode": "automatic",
                        "decision_required": True,
                    },
                }
            )
            request = PipelineDecision(
                tenant=self.owner.tenant,
                pipeline_id="mail",
                revision_id="a" * 32,
                account_id=self.account,
                email_id=self.message_id,
                instructions="Do not answer newsletters.",
            )
            calls: list[str] = []

            def decide(incoming: httpx.Request) -> httpx.Response:
                calls.append(incoming.url.path)
                return httpx.Response(
                    200,
                    json={
                        "should_reply": False,
                        "body": "",
                        "summary": "No answer required",
                        "category": "information",
                        "model": "local",
                    },
                )

            with (
                patch.object(
                    MailService,
                    "pipeline_policies",
                    new=AsyncMock(return_value=(policy,)),
                ),
                patch.object(MailService, "authorize", new=AsyncMock()),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(decide)
                ) as client:
                    service._http = client
                    decision = await service.pipeline_decide(request)
                    self.assertEqual(decision["status"], "not_required")
                    self.assertEqual(
                        await service.pipeline_decide(request), decision
                    )
                    output = PipelineMail(
                        **request.model_dump(
                            exclude={
                                "instructions",
                                "model_alias",
                                "include_attachments",
                                "decision_id",
                            }
                        ),
                        decision_id=decision["decision_id"],
                    )
                    self.assertEqual(
                        await service.pipeline_reply(output),
                        {"status": "not_required"},
                    )
                    with self.assertRaises(MailError):
                        await service.pipeline_reply(
                            output.model_copy(update={"decision_id": None})
                        )
            self.assertEqual(calls, ["/utils/draft"])
            self.assertEqual(self.store.drafts(self.owner, self.account), [])
            transport.send.assert_not_called()

        asyncio.run(run())

    def test_scribe_selected_reply_is_sent_once_and_foreign_decisions_are_rejected(
        self,
    ) -> None:
        from galadril_plugins.utils.models import DeployedMailPipeline
        from galadril_plugins.utils.service import (
            PipelineDecision,
            PipelineMail,
        )

        async def run() -> None:
            self.store.mark_ingested(self.owner, self.account, self.message_id)
            transport = MagicMock(spec=MailTransport)
            service = MailService(
                self.store, transport, "k" * 48, "http://scribe", {}
            )
            policy = DeployedMailPipeline.model_validate(
                {
                    "pipeline_id": "mail",
                    "revision_id": "a" * 32,
                    "policy": {
                        "sender_account_id": self.account,
                        "mode": "automatic",
                        "decision_required": True,
                    },
                }
            )
            request = PipelineDecision(
                tenant=self.owner.tenant,
                pipeline_id="mail",
                revision_id="a" * 32,
                account_id=self.account,
                email_id=self.message_id,
                instructions="Answer questions briefly.",
            )
            with (
                patch.object(
                    MailService,
                    "pipeline_policies",
                    new=AsyncMock(return_value=(policy,)),
                ),
                patch.object(MailService, "authorize", new=AsyncMock()),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            200,
                            json={
                                "should_reply": True,
                                "body": "A useful answer",
                                "summary": "Question",
                                "category": "support",
                                "model": "local",
                            },
                        )
                    )
                ) as client:
                    service._http = client
                    decision = await service.pipeline_decide(request)
                    output = PipelineMail(
                        tenant=self.owner.tenant,
                        pipeline_id="mail",
                        revision_id="a" * 32,
                        account_id=self.account,
                        email_id=self.message_id,
                        draft_id=decision["id"],
                        decision_id=decision["decision_id"],
                    )
                    with self.assertRaisesRegex(MailError, "FORBIDDEN"):
                        await service.pipeline_reply(
                            output.model_copy(update={"decision_id": None})
                        )
                    with self.assertRaises(MailError):
                        await service.pipeline_reply(
                            output.model_copy(update={"decision_id": "f" * 64})
                        )
                    self.assertEqual(
                        await service.pipeline_reply(output), {"status": "sent"}
                    )
                    self.assertEqual(
                        await service.pipeline_reply(output),
                        {"status": "already_processed"},
                    )
            transport.send.assert_called_once()

        asyncio.run(run())

    def test_model_message_read_omits_binary_attachment_content(self) -> None:
        async def run() -> None:
            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            value = await service.operate(
                self.owner,
                Operation(
                    operation="message",
                    account=self.account,
                    message=self.message_id,
                ),
                delegated=True,
            )
            self.assertEqual(value["attachments"][0]["content"], "")
            self.assertEqual(value["attachments"][0]["name"], "note.txt")

        asyncio.run(run())

    def test_reply_source_is_resolved_by_server_and_rechecked_before_send(
        self,
    ) -> None:
        value = DraftInput.model_validate(
            {
                "to": ["sender@example.org"],
                "subject": "Re: Question",
                "body": "Reply",
                "reply_source": {
                    "account": self.account,
                    "message": self.message_id,
                },
                "reply_to_message_id": "<forged@example.org>",
            }
        )
        self.store.grant(
            self.owner,
            self.account,
            "writer",
            ("read", "use", "approve", "send"),
        )
        writer = Principal(tenant="one", user="writer")
        draft = self.store.save_draft(writer, self.account, None, None, value)
        self.assertEqual(
            draft["content"]["reply_to_message_id"], self.message.message_id
        )
        self.assertEqual(
            draft["content"]["reply_source"],
            {"account": self.account, "message": self.message_id},
        )
        self.store.approve(writer, self.account, draft["id"], draft["revision"])
        self.store.grant(
            self.owner, self.account, "writer", ("use", "approve", "send")
        )
        with self.assertRaises(MailError):
            self.store.claim_send(
                writer, self.account, draft["id"], draft["revision"]
            )
        self.assertEqual(
            self.store.draft(self.owner, self.account, draft["id"])["status"],
            "approved",
        )
        with self.assertRaises(MailError):
            self.store.save_draft(
                Principal(tenant="other", user="owner"),
                self.account,
                None,
                None,
                value,
            )

    def test_rfc_parent_survives_without_references_header(self) -> None:
        message = parse_message(
            b"From: sender@example.org\r\nMessage-ID: <child@example.org>\r\nIn-Reply-To: <source@example.org>\r\n\r\nReply"
        )
        self.assertEqual(message.in_reply_to, "<source@example.org>")
        child = self.store.record_message(
            self.owner, self.account, "INBOX", 1, 2, message
        )
        thread = self.store.thread(self.owner, self.account, child)
        self.assertEqual(
            thread["messages"][1]["parent"], self.message.message_id
        )

    def test_history_cutoff_is_durable_across_batches_and_validity_changes(
        self,
    ) -> None:
        cutoff = self.store.history_boundary(
            self.owner, self.account, "Archive", 7, 100
        )
        self.assertEqual(cutoff, 99)
        self.store.advance(self.owner, self.account, "Archive", 7, 25)
        self.assertEqual(
            self.store.history_boundary(
                self.owner, self.account, "Archive", 7, 120
            ),
            99,
        )
        self.assertEqual(
            self.store.history_boundary(
                self.owner, self.account, "Archive", 8, 50
            ),
            49,
        )

    def test_historical_mail_never_enters_automatic_reply_queue(self) -> None:
        historical = self.message.model_copy(
            update={"historical": True, "message_id": "<old@example.org>"}
        )
        self.store.record_message(
            self.owner, self.account, "Archive", 2, 1, historical
        )
        self.assertNotIn(
            "<old@example.org>",
            [
                message.message_id
                for _, message in self.store.pending(self.owner, self.account)
            ],
        )
        self.assertTrue(
            all(
                item["message"]["message_id"] != "<old@example.org>"
                for item in self.store.messages(
                    self.owner,
                    self.account,
                    pending=True,
                    include_history=False,
                )
            )
        )

    def test_delegated_thread_and_attachment_reads_enforce_live_mailbox_rights(
        self,
    ) -> None:
        async def run() -> None:
            self.store.grant(self.owner, self.account, "reader", ("read",))
            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(service)),
                base_url="http://mail",
            ) as client:
                for user, tenant, status in (
                    ("reader", "one", 200),
                    ("outsider", "one", 403),
                    ("owner", "other", 403),
                ):
                    for operation in (
                        {
                            "operation": "thread",
                            "account": self.account,
                            "message": self.message_id,
                        },
                        {
                            "operation": "attachment",
                            "account": self.account,
                            "message": self.message_id,
                            "attachment": 0,
                        },
                    ):
                        response = await client.post(
                            "/operation",
                            json=operation,
                            headers={
                                "x-utils-key": "k" * 48,
                                "x-utils-tenant": tenant,
                                "x-utils-user": user,
                                "x-utils-delegated": "true",
                            },
                        )
                        self.assertEqual(
                            response.status_code, status, response.text
                        )
                        if (
                            status == 200
                            and operation["operation"] == "attachment"
                        ):
                            self.assertIn(
                                "Private attachment", response.json()["text"]
                            )
                            self.assertNotIn("content", response.json())
                with self.assertRaises(ValidationError):
                    Operation(
                        operation="attachment",
                        account=self.account,
                        message=self.message_id,
                        attachment=-1,
                    )

        asyncio.run(run())

    def test_pdf_attachment_uses_native_parser_without_embedding_or_publication(
        self,
    ) -> None:
        import base64
        import json

        from galadril_plugins.utils.models import Attachment

        async def run() -> None:
            message = self.message.model_copy(
                update={
                    "message_id": "<pdf@example.org>",
                    "attachments": (
                        Attachment(
                            name="report.pdf",
                            content_type="application/pdf",
                            content=base64.b64encode(b"%PDF-1.7 test").decode(),
                        ),
                    ),
                }
            )
            key = self.store.record_message(
                self.owner, self.account, "INBOX", 1, 2, message
            )
            calls: list[str] = []

            def parse(request: httpx.Request) -> httpx.Response:
                calls.append(request.url.path)
                self.assertEqual(request.headers["x-utils-key"], "k" * 48)
                self.assertEqual(
                    json.loads(request.content)["mime"], "application/pdf"
                )
                return httpx.Response(
                    200,
                    json={
                        "parser_revision": "native",
                        "units": [{"text": "Evidence", "page": 1}],
                        "warnings": [],
                    },
                )

            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(parse)
            ) as client:
                service._http = client
                result = await service.attachment_text(
                    self.owner, self.account, key, 0
                )
            self.assertEqual(result["text"], "Evidence")
            self.assertEqual(calls, ["/utils/parse"])
            self.assertFalse(
                self.store.messages(self.owner, self.account)[0]["ingested"]
            )

        asyncio.run(run())

    def test_analysis_context_contains_thread_and_attachment_without_sending(
        self,
    ) -> None:
        async def run() -> None:
            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            result = await service.context_snapshot(
                self.owner,
                self.account,
                self.message_id,
                include_attachments=True,
            )
            self.assertEqual(result["messages"][0]["body"], "Question")
            self.assertIn(
                "Private attachment",
                result["messages"][0]["attachments"][0]["text"],
            )
            self.assertEqual(len(result["snapshot_id"]), 64)
            self.assertEqual(self.store.drafts(self.owner, self.account), [])

        asyncio.run(run())

    def test_thread_chronology_uses_message_dates_instead_of_import_order(
        self,
    ) -> None:
        late = self.message.model_copy(
            update={
                "message_id": "<late@example.org>",
                "references": "<source@example.org>",
                "date": "Tue, 20 Oct 2026 12:00:00 +0000",
            }
        )
        early = self.message.model_copy(
            update={
                "message_id": "<early@example.org>",
                "references": "<source@example.org>",
                "date": "Mon, 19 Oct 2026 12:00:00 +0000",
            }
        )
        self.store.record_message(
            self.owner, self.account, "Archive", 1, 2, late
        )
        self.store.record_message(
            self.owner, self.account, "INBOX", 1, 3, early
        )
        thread = self.store.thread(self.owner, self.account, self.message_id)
        self.assertEqual(
            [row["message_id"] for row in thread["messages"]],
            [self.message.message_id, early.message_id, late.message_id],
        )

    def test_source_only_pipeline_can_read_ingested_evidence_without_reply_authority(
        self,
    ) -> None:
        from galadril_plugins.utils.models import DeployedMailPipeline

        async def run() -> None:
            self.store.mark_ingested(self.owner, self.account, self.message_id)
            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            policy = DeployedMailPipeline(
                pipeline_id="analysis", revision_id="a" * 32, policy=None
            )
            with (
                patch.object(
                    MailService,
                    "pipeline_policies",
                    new=AsyncMock(return_value=(policy,)),
                ),
                patch.object(MailService, "authorize", new=AsyncMock()),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=create_app(service)),
                    base_url="http://mail",
                ) as client:
                    payload = {
                        "tenant": self.owner.tenant,
                        "account_id": self.account,
                        "email_id": self.message_id,
                        "pipeline_id": "analysis",
                        "revision_id": "a" * 32,
                    }
                    response = await client.post(
                        "/pipeline/context",
                        json=payload,
                        headers={"x-utils-key": "k" * 48},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn(
                        "Private attachment",
                        response.json()["messages"][0]["attachments"][0][
                            "text"
                        ],
                    )
                    denied = await client.post(
                        "/pipeline/reply",
                        json=payload,
                        headers={"x-utils-key": "k" * 48},
                    )
                    self.assertEqual(denied.status_code, 403)
            self.assertEqual(self.store.drafts(self.owner, self.account), [])

        asyncio.run(run())

    def test_reused_rfc_message_id_cannot_select_another_sources_prepared_reply(
        self,
    ) -> None:
        import hashlib

        from galadril_plugins.utils.models import ReplyComposition, ReplySource

        async def run() -> None:
            original = DraftInput(
                to=("sender@example.org",),
                subject="Re: Question",
                body="Original answer",
                reply_source=ReplySource(
                    account=self.account, message=self.message_id
                ),
            )
            self.store.save_draft(
                self.owner,
                self.account,
                None,
                None,
                original,
                automatic=True,
                source_id=hashlib.sha256(
                    self.message.message_id.encode()
                ).hexdigest(),
            )
            other = self.store.record_message(
                self.owner,
                self.account,
                "Archive",
                2,
                9,
                self.message.model_copy(
                    update={
                        "body": "A different question with a reused Message-ID"
                    }
                ),
            )
            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "should_reply": True,
                            "body": "A different answer",
                            "summary": "Question",
                            "category": "support",
                            "model": "local",
                        },
                    )
                )
            ) as client:
                service._http = client
                with self.assertRaises(MailError):
                    await service.generate(
                        self.owner,
                        self.account,
                        other,
                        automatic=True,
                        sender_account=self.account,
                        composition=ReplyComposition(decide_reply=True),
                    )

        asyncio.run(run())

    def test_context_budget_preserves_source_and_reports_omitted_evidence(
        self,
    ) -> None:
        import base64

        from galadril_plugins.utils.models import Attachment

        async def run() -> None:
            source = self.message.model_copy(
                update={
                    "message_id": "<budget@example.org>",
                    "body": "Source " * 1000,
                    "attachments": tuple(
                        Attachment(
                            name=f"note-{index}.txt",
                            content_type="text/plain",
                            content=base64.b64encode(
                                b"Attachment evidence " * 1000
                            ).decode(),
                        )
                        for index in range(3)
                    ),
                }
            )
            key = self.store.record_message(
                self.owner, self.account, "INBOX", 1, 20, source
            )
            for index in range(9):
                self.store.record_message(
                    self.owner,
                    self.account,
                    "Archive",
                    1,
                    index + 1,
                    self.message.model_copy(
                        update={
                            "message_id": f"<context-{index}@example.org>",
                            "references": "<budget@example.org>",
                            "thread_id": "<budget@example.org>",
                            "body": "Previous context " * 1000,
                        }
                    ),
                )
            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            result = await service.context_snapshot(
                self.owner, self.account, key, include_attachments=True
            )
            self.assertEqual(result["messages"][0]["email_id"], key)
            self.assertTrue(result["messages"][0]["body_truncated"])
            self.assertTrue(result["messages"][0]["attachments_truncated"])
            self.assertTrue(
                result["messages"][0]["attachments"][0]["truncated"]
            )
            self.assertEqual(len(result["messages"]), 8)
            self.assertIn("partial_thread", result["warnings"])
            self.assertIn("context_truncated", result["warnings"])
            selected = await service.context_snapshot(
                self.owner, self.account, key, include_thread=False
            )
            self.assertEqual(len(selected["messages"]), 1)
            self.assertEqual(selected["messages"][0]["attachments"], [])

        asyncio.run(run())

    def test_attachment_pages_unsupported_files_and_revocation_are_explicit(
        self,
    ) -> None:
        import base64

        from galadril_plugins.utils.models import Attachment

        async def run() -> None:
            source = self.message.model_copy(
                update={
                    "message_id": "<pages@example.org>",
                    "attachments": (
                        Attachment(
                            name="large.csv",
                            content_type="text/csv",
                            content=base64.b64encode(b"x" * 17000).decode(),
                        ),
                        Attachment(
                            name="archive.zip",
                            content_type="application/zip",
                            content="AA==",
                        ),
                    ),
                }
            )
            key = self.store.record_message(
                self.owner, self.account, "INBOX", 1, 2, source
            )
            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            first = await service.attachment_text(
                self.owner, self.account, key, 0
            )
            second = await service.attachment_text(
                self.owner, self.account, key, 0, first["next_offset"]
            )
            self.assertEqual(len(first["text"]) + len(second["text"]), 17000)
            self.assertEqual(second["next_offset"], 0)
            self.assertEqual(
                (
                    await service.attachment_text(
                        self.owner, self.account, key, 1
                    )
                )["warnings"],
                ["unsupported_format"],
            )
            with self.assertRaises(MailError):
                await service.attachment_text(self.owner, self.account, key, 2)
            reader = Principal(tenant="one", user="reader")
            self.store.grant(self.owner, self.account, "reader", ("read",))
            self.store.grant(self.owner, self.account, "reader", ())
            with self.assertRaises(MailError):
                await service.attachment_text(reader, self.account, key, 0)

        asyncio.run(run())

    def test_naive_message_dates_have_the_same_chronology_as_utc(self) -> None:
        from galadril_plugins.utils.store import _message_time

        self.assertEqual(
            _message_time("Mon, 19 Oct 2026 12:00:00"),
            _message_time("Mon, 19 Oct 2026 12:00:00 +0000"),
        )
        self.assertEqual(_message_time("not a date"), 0.0)

    def test_pipeline_service_origin_and_options_cannot_override_trusted_scope(
        self,
    ) -> None:
        from galadril_plugins.utils.models import (
            EmailContextOptions,
            ReplyDecisionOptions,
        )
        from galadril_plugins.utils.pipeline import _mail_endpoint

        for endpoint in (
            "",
            "file:///mail",
            "http://user:private@example.org",
            "http://example.org/path",
            "http://example.org?token=private",
            "http://example.org#fragment",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                _mail_endpoint(endpoint)
        for contract in (EmailContextOptions, ReplyDecisionOptions):
            with self.assertRaises(ValidationError):
                contract.model_validate({"tenant": "foreign"})

    def test_chat_reads_long_messages_in_bounded_pages_without_binary_data(
        self,
    ) -> None:
        async def run() -> None:
            body = "Evidence " * 4000
            source = self.store.record_message(
                self.owner,
                self.account,
                "INBOX",
                1,
                99,
                self.message.model_copy(
                    update={"message_id": "<long@example.org>", "body": body}
                ),
            )
            service = MailService(
                self.store, MailTransport(), "k" * 48, "http://scribe", {}
            )
            first = await service.operate(
                self.owner,
                Operation(
                    operation="message", account=self.account, message=source
                ),
                delegated=True,
            )
            self.assertEqual(len(first["body"]), 16384)
            second = await service.operate(
                self.owner,
                Operation(
                    operation="message",
                    account=self.account,
                    message=source,
                    offset=first["next_offset"],
                ),
                delegated=True,
            )
            self.assertEqual(first["body"] + second["body"], body[:32768])
            self.assertEqual(second["attachments"][0]["content"], "")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
