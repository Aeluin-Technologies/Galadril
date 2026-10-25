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
