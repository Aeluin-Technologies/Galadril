"""Regressions for independently authorized encrypted email connections."""

import tempfile
import unittest
from pathlib import Path

from galadril_plugins.utils.models import (
    ConnectionInput,
    Credentials,
    Principal,
)
from galadril_plugins.utils.store import MailError, MailStore
from pydantic import SecretStr


class ConnectionSecurity(unittest.TestCase):
    def test_default_signature_contains_no_visible_reference(self) -> None:
        from galadril_plugins.utils.models import ResponseProfile
        self.assertEqual(ResponseProfile(model="local").signature, "")
    def test_message_and_thread_ids_remain_searchable_without_visible_references(
        self,
    ) -> None:
        from galadril_plugins.utils.mime import parse_message

        root = parse_message(
            b"From: sender@example.org\r\nTo: owner@example.org\r\nMessage-ID: <root-independent@example.org>\r\nSubject: Original\r\n\r\nNo visible reference"
        )
        reply = parse_message(
            b"From: sender@example.org\r\nTo: owner@example.org\r\nMessage-ID: <reply-independent@example.org>\r\nReferences: <root-independent@example.org>\r\nIn-Reply-To: <root-independent@example.org>\r\nSubject: Different title\r\n\r\nNo visible reference"
        )
        self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 301, root
        )
        reply_id = self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 302, reply
        )
        matches = self.store.message_page(
            self.owner, self.account["id"], query=reply.message_id
        )["items"]
        self.assertEqual([item["id"] for item in matches], [reply_id])
        self.assertEqual(
            len(
                self.store.message_page(
                    self.owner, self.account["id"], query=root.thread_id
                )["items"]
            ),
            2,
        )
        thread = self.store.thread(self.owner, self.account["id"], reply_id)
        self.assertEqual(thread["thread_id"], root.message_id)
        self.assertEqual(len(thread["messages"]), 2)

    def test_proposal_delivery_status_survives_reload_without_revealing_the_sender(
        self,
    ) -> None:
        from galadril_plugins.utils.models import DraftInput

        draft_id = "a" * 32
        self.assertEqual(
            self.store.proposal_status(self.owner, draft_id), {"status": "new"}
        )
        draft = self.store.save_draft(
            self.owner,
            self.account["id"],
            draft_id,
            None,
            DraftInput(
                to=("recipient@example.org",), subject="Hello", body="Review"
            ),
        )
        self.store.approve(
            self.owner, self.account["id"], draft_id, draft["revision"]
        )
        self.store.claim_send(
            self.owner, self.account["id"], draft_id, draft["revision"]
        )
        self.store.finish_send(self.owner, self.account["id"], draft_id, True)
        result = self.store.proposal_status(self.owner, draft_id)
        self.assertEqual(result, {"status": "sent"})
        with self.assertRaises(MailError):
            self.store.proposal_status(
                Principal(tenant=self.owner.tenant, user="other"), draft_id
            )

    def test_mail_search_ignores_accents_and_matches_words_in_any_order(
        self,
    ) -> None:
        from galadril_plugins.utils.mime import parse_message

        message = parse_message(
            "From: sender@example.org\r\nTo: support@example.org\r\nMessage-ID: <search@example.org>\r\nSubject: Changement de contenu de Mathématiques L3\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nProgramme modifié".encode()
        )
        self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 1, message
        )
        for query in (
            "mathematiques",
            "L3 MATHEMATIQUES",
            "  programme   modifie  ",
            "mathematiqes changment",
            "mathématiqeus",
            "contennu",
        ):
            self.assertEqual(
                len(
                    self.store.message_page(
                        self.owner, self.account["id"], query=query
                    )["items"]
                ),
                1,
            )
        self.assertEqual(
            self.store.message_page(
                self.owner, self.account["id"], query="unknown"
            )["items"],
            [],
        )
        self.assertEqual(
            self.store.message_page(self.owner, self.account["id"], query="L4")[
                "items"
            ],
            [],
        )

    def test_thread_references_follow_message_ids_and_survive_redelivery(
        self,
    ) -> None:
        from galadril_plugins.utils.mime import parse_message
        from galadril_plugins.utils.models import DraftInput

        root = parse_message(
            b"From: sender@example.org\r\nTo: support@example.org\r\nMessage-ID: <root@example.org>\r\nSubject: Help\r\n\r\nQuestion"
        )
        root_id = self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 101, root
        )
        first = self.store.message(self.owner, self.account["id"], root_id)
        self.assertEqual(first.thread_reference, "<root@example.org>/1")
        reply = parse_message(
            b"From: sender@example.org\r\nTo: support@example.org\r\nMessage-ID: <reply@example.org>\r\nReferences: <root@example.org>\r\nIn-Reply-To: <root@example.org>\r\nSubject: Re: Help\r\n\r\nMore"
        )
        reply_id = self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 102, reply
        )
        self.store.record_message(
            self.owner, self.account["id"], "Archive", 1, 10, reply
        )
        second = self.store.message(self.owner, self.account["id"], reply_id)
        self.assertEqual(second.thread_reference, "<root@example.org>/2")
        outgoing = self.store.save_draft(
            self.owner,
            self.account["id"],
            None,
            None,
            DraftInput(
                to=("sender@example.org",),
                subject="Reply",
                body="Reply",
                reply_to_message_id=reply.message_id,
                references=reply.references,
            ),
            source_reference=second.reference,
        )
        self.assertEqual(
            outgoing["content"]["thread_reference"], "<root@example.org>/3"
        )
        self.store.mark_ingested(
            self.owner,
            self.account["id"],
            root_id,
            source_key="tenant-a/raw/email/source.json",
        )
        graph = self.store.thread(self.owner, self.account["id"], root_id)
        self.assertEqual(
            graph["messages"][0]["source_key"], "tenant-a/raw/email/source.json"
        )
        self.assertEqual(
            [item["number"] for item in graph["messages"]], [1, 2, 3]
        )
        self.assertNotIn("private-password", str(graph))
        self.assertNotIn("private-login", str(graph))
        with self.assertRaises(MailError):
            self.store.thread(self.outsider, self.account["id"], root_id)

    def test_draft_pages_are_bounded_and_keep_the_account_boundary(
        self,
    ) -> None:
        from galadril_plugins.utils.models import DraftInput

        for number in range(26):
            self.store.save_draft(
                self.owner,
                self.account["id"],
                None,
                None,
                DraftInput(
                    to=("to@example.org",),
                    subject=f"Draft {number}",
                    body="Body",
                ),
            )
        first = self.store.draft_page(self.owner, self.account["id"])
        self.assertEqual(len(first["items"]), 25)
        self.assertGreater(first["next_before"], 0)
        second = self.store.draft_page(
            self.owner, self.account["id"], before=first["next_before"]
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["next_before"], 0)
        self.assertFalse(
            {item["id"] for item in first["items"]}
            & {item["id"] for item in second["items"]}
        )
        with self.assertRaises(MailError):
            self.store.draft_page(self.outsider, self.account["id"])

    def test_receiving_our_own_sent_message_keeps_its_verified_reference(
        self,
    ) -> None:
        from galadril_plugins.utils.mime import compose_reply, parse_message
        from galadril_plugins.utils.models import DraftInput

        config, _ = self.store.connection(self.owner, self.account["id"], "use")
        draft = self.store.save_draft(
            self.owner,
            self.account["id"],
            None,
            None,
            DraftInput(to=(config.address,), subject="Test", body="Test"),
        )
        self.store.approve(
            self.owner, self.account["id"], draft["id"], draft["revision"]
        )
        content = self.store.claim_send(
            self.owner, self.account["id"], draft["id"], draft["revision"]
        )
        self.store.finish_send(
            self.owner, self.account["id"], draft["id"], True
        )
        message = parse_message(
            compose_reply(config.address, content, draft["delivery_id"])
        )
        key = self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 42, message
        )
        received = self.store.message(self.owner, self.account["id"], key)
        self.assertEqual(received.reference, content.reference)
        forged = message.model_copy(
            update={"message_id": "<unrecognized@example.org>"}
        )
        other = self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 43, forged
        )
        self.assertNotEqual(
            self.store.message(self.owner, self.account["id"], other).reference,
            content.reference,
        )

    def test_reference_survives_collection_replay_and_reply_edits(self) -> None:
        from galadril_plugins.utils.mime import parse_message
        from galadril_plugins.utils.models import DraftInput

        message = parse_message(
            b"From: sender@example.org\r\nTo: support@example.org\r\nMessage-ID: <reference@example.org>\r\nSubject: Help\r\n\r\nQuestion"
        )
        key = self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 1, message
        )
        repeated = self.store.record_message(
            self.owner, self.account["id"], "INBOX", 1, 1, message
        )
        self.assertEqual(key, repeated)
        collected = self.store.message(self.owner, self.account["id"], key)
        self.assertEqual(collected.reference, "GDR-" + key.upper())
        self.assertEqual(
            len(
                self.store.message_page(
                    self.owner, self.account["id"], query=collected.reference
                )["items"]
            ),
            1,
        )
        draft = self.store.save_draft(
            self.owner,
            self.account["id"],
            None,
            None,
            DraftInput(
                to=("sender@example.org",), subject="Reply", body="Reply"
            ),
            source_reference=collected.reference,
        )
        self.assertEqual(draft["content"]["reference"], collected.reference)
        edited = self.store.save_draft(
            self.owner,
            self.account["id"],
            draft["id"],
            draft["revision"],
            DraftInput(
                to=("sender@example.org",),
                subject="Edited",
                body="Edited",
                reference="GDR-" + "F" * 32,
            ),
        )
        self.assertEqual(edited["content"]["reference"], collected.reference)

    def test_one_proposal_cannot_be_resent_through_another_account(
        self,
    ) -> None:
        from galadril_plugins.utils.models import DraftInput

        second = self.store.create(
            self.owner,
            self.value.model_copy(update={"address": "other@example.org"}),
            Credentials(username="other", password=SecretStr("private")),
        )
        draft = DraftInput(
            to=("recipient@example.org",), subject="Test", body="Test"
        )
        identifier = "d" * 32
        self.store.save_draft(
            self.owner, self.account["id"], identifier, None, draft
        )
        with self.assertRaisesRegex(MailError, "CONFLICT"):
            self.store.save_draft(
                self.owner, second["id"], identifier, None, draft
            )

    def test_password_only_rotation_preserves_private_login_and_blocks_redirection(
        self,
    ) -> None:
        from galadril_plugins.utils.models import PasswordReplacement

        self.store.grant(self.owner, self.account["id"], "editor", ("edit",))
        config, original = self.store.connection(
            self.owner, self.account["id"], "use"
        )
        updated = self.store.update(
            self.editor,
            self.account["id"],
            1,
            config,
            PasswordReplacement(password=SecretStr("replacement")),
        )
        current = self.store.reveal(self.owner, self.account["id"])
        self.assertEqual(current.username, original.username)
        self.assertEqual(current.password.get_secret_value(), "replacement")
        with self.assertRaises(MailError):
            self.store.reveal(self.editor, self.account["id"])
        with self.assertRaises(MailError):
            self.store.update(
                self.editor,
                self.account["id"],
                updated["revision"],
                config.model_copy(update={"imap_host": "attacker.example.org"}),
                PasswordReplacement(password=SecretStr("new-secret")),
            )

    def test_credentials_cannot_inject_protocol_commands(self) -> None:
        with self.assertRaises(ValueError):
            Credentials(
                username="login",
                password=SecretStr("password\r\nUID MOVE 1 INBOX"),
            )

    def test_modification_right_exposes_only_editable_metadata(self) -> None:
        self.store.grant(self.owner, self.account["id"], "editor", ("edit",))
        visible = self.store.accounts(self.editor)
        self.assertEqual(len(visible), 1)
        self.assertNotIn("private-password", str(visible))
        self.assertFalse(visible[0]["can_manage_access"])
        self.assertTrue(self.store.accounts(self.owner)[0]["can_manage_access"])
        with self.assertRaises(MailError):
            self.store.reveal(self.editor, self.account["id"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "mail.db"
        self.store = MailStore(self.path, {"v1": bytes(range(32))}, "v1")
        self.owner = Principal(tenant="tenant-a", user="owner")
        self.editor = Principal(tenant="tenant-a", user="editor")
        self.outsider = Principal(tenant="tenant-b", user="owner")
        self.value = ConnectionInput(
            name="Support",
            address="support@example.org",
            imap_host="imap.example.org",
            smtp_host="smtp.example.org",
        )
        self.secrets = Credentials(
            username="private-login", password=SecretStr("private-password")
        )
        self.account = self.store.create(self.owner, self.value, self.secrets)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_pipeline_access_does_not_grant_connection_or_secret_access(
        self,
    ) -> None:
        self.assertEqual(self.store.accounts(self.editor), [])
        for actor in (self.editor, self.outsider):
            with self.assertRaises(MailError):
                self.store.reveal(actor, self.account["id"])
            with self.assertRaises(MailError):
                self.store.update(
                    actor, self.account["id"], 1, self.value, None
                )

    def test_editor_rotates_without_reading_old_credentials(self) -> None:
        identifier = self.account["id"]
        self.store.grant(
            self.owner, identifier, "editor", ("view", "edit", "delete")
        )
        public = self.store.accounts(self.editor)
        self.assertNotIn("private-login", str(public))
        self.assertNotIn("private-password", str(public))
        with self.assertRaises(MailError):
            self.store.reveal(self.editor, identifier)
        self.store.update(
            self.editor,
            identifier,
            1,
            self.value,
            Credentials(
                username="new-login", password=SecretStr("new-password")
            ),
        )
        self.assertEqual(
            self.store.reveal(
                self.owner, identifier
            ).password.get_secret_value(),
            "new-password",
        )
        self.store.delete(self.editor, identifier, 2)
        self.assertEqual(self.store.accounts(self.owner), [])

    def test_secrets_and_mail_are_encrypted_on_disk_and_bound_to_account(
        self,
    ) -> None:
        disk = self.path.read_bytes()
        self.assertNotIn(b"private-login", disk)
        self.assertNotIn(b"private-password", disk)
        with self.store.database() as db:
            ciphertext = db.execute("SELECT secrets FROM accounts").fetchone()[
                0
            ]
        with self.assertRaises(MailError):
            self.store.decrypt(
                "tenant-b", self.account["id"], "credentials", ciphertext
            )
        reopened = MailStore(self.path, {"v1": bytes(range(32))}, "v1")
        self.assertEqual(
            reopened.reveal(self.owner, self.account["id"]).username,
            "private-login",
        )

    def test_revision_conflicts_and_grant_escalation_fail_closed(self) -> None:
        identifier = self.account["id"]
        self.store.grant(self.owner, identifier, "editor", ("edit",))
        with self.assertRaises(MailError):
            self.store.grant(self.editor, identifier, "editor", ("reveal",))
        with self.assertRaises(MailError):
            self.store.update(self.owner, identifier, 99, self.value, None)
        self.assertEqual(
            self.store.reveal(self.owner, identifier).username, "private-login"
        )

    def test_editor_cannot_redirect_saved_credentials_to_another_server(
        self,
    ) -> None:
        identifier = self.account["id"]
        self.store.grant(self.owner, identifier, "editor", ("edit",))
        with self.assertRaises(MailError):
            self.store.update(
                self.editor,
                identifier,
                1,
                self.value.model_copy(
                    update={"smtp_host": "attacker.example.org"}
                ),
                None,
            )


if __name__ == "__main__":
    unittest.main()
