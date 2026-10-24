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
