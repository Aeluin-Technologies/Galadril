"""Provider boundaries reject secret redirection and unbounded remote payloads."""

import imaplib
import unittest
from unittest.mock import MagicMock, patch

from galadril_plugins.utils.models import ConnectionInput, Credentials
from galadril_plugins.utils.store import MailError
from galadril_plugins.utils.transport import (
    MailTransport,
    SafeIMAP,
    resolve_host,
)
from pydantic import SecretStr


class TransportSecurity(unittest.TestCase):
    def test_sender_display_name_is_encoded_without_changing_the_address(
        self,
    ) -> None:
        from email import policy
        from email.parser import BytesParser

        from galadril_plugins.utils.mime import compose_reply
        from galadril_plugins.utils.models import DraftInput

        message = BytesParser(policy=policy.default).parsebytes(
            compose_reply(
                "me@example.org",
                DraftInput(to=("to@example.org",), subject="Test", body="Body"),
                "<id@example.org>",
                sender_name="Équipe Anthony",
            )
        )
        self.assertEqual(
            message["From"].addresses[0].display_name, "Équipe Anthony"
        )
        self.assertEqual(
            message["From"].addresses[0].addr_spec, "me@example.org"
        )
        with self.assertRaises(ValueError):
            ConnectionInput(
                name="Mailbox",
                address="me@example.org",
                imap_host="imap.example.org",
                smtp_host="smtp.example.org",
                sender_name="Anthony\r\nBcc: other@example.org",
            )

    def test_reference_can_be_displayed_as_a_code_block(self) -> None:
        from email import policy
        from email.parser import BytesParser

        from galadril_plugins.utils.mime import compose_reply
        from galadril_plugins.utils.models import DraftInput

        ref = "GDR-" + "A" * 32
        raw = compose_reply(
            "me@example.org",
            DraftInput(
                to=("to@example.org",),
                subject="Test",
                body="Body",
                reference=ref,
            ),
            "<id@example.org>",
            signature_html="<p>Bien cordialement,<br>Anthony CHENOT--NOGUES</p><pre><code>Réf. : {{ ref }}</code></pre>",
        )
        html = (
            BytesParser(policy=policy.default)
            .parsebytes(raw)
            .get_body(preferencelist=("html",))
            .get_content()
        )
        self.assertIn("<pre><code>Réf. : " + ref + "</code></pre>", html)

    def test_signature_fields_are_escaped_and_reference_is_traceable(
        self,
    ) -> None:
        from email import policy
        from email.parser import BytesParser

        from galadril_plugins.utils.mime import compose_reply
        from galadril_plugins.utils.models import DraftInput, ResponseProfile

        ref = "GDR-" + "A" * 32
        value = DraftInput(
            to=("to@example.org",),
            subject='<img src="https://tracker.example/x">',
            body="Reply",
            reference=ref,
        )
        raw = compose_reply(
            "me@example.org",
            value,
            "<id@example.org>",
            signature_html=ResponseProfile(
                model="local",
                signature="Ref. : <code>{{ ref }}</code><br>{{ subject }}",
            ).signature,
        )
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(message["X-Galadril-Reference"], ref)
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("<code>" + ref + "</code>", html)
        self.assertNotIn("<img", html)
        self.assertIn(
            ref, message.get_body(preferencelist=("plain",)).get_content()
        )
        with self.assertRaises(ValueError):
            ResponseProfile(model="local", signature="{{ arbitrary_code }}")

    def test_html_signature_is_sanitized_and_bcc_never_enters_headers(
        self,
    ) -> None:
        from email import policy
        from email.parser import BytesParser

        from galadril_plugins.utils.mime import compose_reply
        from galadril_plugins.utils.models import DraftInput

        draft = DraftInput(
            to=("to@example.org",),
            bcc=("private@example.org",),
            subject="Hello",
            body="<plain text>",
        )
        raw = compose_reply(
            "me@example.org",
            draft,
            "<id@example.org>",
            signature_html='<b>Regards</b><img src="https://tracker.example/pixel"><script>bad()</script>',
        )
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertIsNone(message.get("Bcc"))
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("<b>Regards</b>", html)
        self.assertIn("&lt;plain text&gt;", html)
        self.assertNotIn("tracker.example", html)
        self.assertNotIn("<script", html)

    def test_authentication_failure_does_not_expose_provider_text(self) -> None:
        from galadril_plugins.utils.transport import _login

        client = MagicMock()
        client.login.side_effect = imaplib.IMAP4.error("private provider text")
        credentials = Credentials(username="user", password=SecretStr("secret"))
        with self.assertRaisesRegex(MailError, "^AUTHENTICATION_FAILED$"):
            _login(client, credentials)

    def test_manual_server_probe_verifies_tls_without_authenticating(
        self,
    ) -> None:
        imap, smtp = MagicMock(), MagicMock()
        with (
            patch(
                "galadril_plugins.utils.transport.resolve_host",
                return_value="8.8.8.8",
            ),
            patch(
                "galadril_plugins.utils.transport.SafeIMAP", return_value=imap
            ) as incoming,
            patch(
                "galadril_plugins.utils.transport.SafeSMTP", return_value=smtp
            ) as outgoing,
        ):
            MailTransport().validate_servers(
                "imap.example.org", "smtp.example.org", 587
            )
        incoming.assert_called_once_with(
            "imap.example.org", 993, "8.8.8.8", timeout=5
        )
        outgoing.assert_called_once_with(
            "smtp.example.org", 587, "8.8.8.8", timeout=5
        )
        imap.login.assert_not_called()
        imap.authenticate.assert_not_called()
        smtp.login.assert_not_called()
        smtp.send_message.assert_not_called()

    def test_initial_collection_can_start_with_new_mail_only(self) -> None:
        config = ConnectionInput(
            name="Mail",
            address="me@example.org",
            imap_host="imap.example.org",
            smtp_host="smtp.example.org",
        )
        client = MagicMock()
        client.__enter__.return_value = client
        client.select.return_value = ("OK", [])
        client.response.side_effect = [
            ("UIDVALIDITY", [b"7"]),
            ("UIDNEXT", [b"100001"]),
        ]
        advance = MagicMock()
        with (
            patch(
                "galadril_plugins.utils.transport.SafeIMAP", return_value=client
            ),
            patch(
                "galadril_plugins.utils.transport.resolve_host",
                return_value="8.8.8.8",
            ),
        ):
            count = MailTransport().sync(
                config,
                Credentials(username="login", password=SecretStr("password")),
                lambda folder: (None, 0),
                MagicMock(),
                advance,
            )
        self.assertEqual(count, 0)
        client.uid.assert_not_called()
        advance.assert_called_once_with("INBOX", 7, 100000)

    def test_sparse_history_advances_bounded_uid_windows(self) -> None:
        config = ConnectionInput(
            name="Mail",
            address="me@example.org",
            imap_host="imap.example.org",
            smtp_host="smtp.example.org",
            initial_sync="all",
        )
        client = MagicMock()
        client.__enter__.return_value = client
        client.select.return_value = ("OK", [])
        client.response.side_effect = [
            ("UIDVALIDITY", [b"7"]),
            ("UIDNEXT", [b"100001"]),
        ]
        client.uid.return_value = ("OK", [b""])
        advance = MagicMock()
        with (
            patch(
                "galadril_plugins.utils.transport.SafeIMAP", return_value=client
            ),
            patch(
                "galadril_plugins.utils.transport.resolve_host",
                return_value="8.8.8.8",
            ),
        ):
            MailTransport().sync(
                config,
                Credentials(username="login", password=SecretStr("password")),
                lambda folder: (7, 0),
                MagicMock(),
                advance,
            )
        client.uid.assert_called_once_with("search", "UID", "1:4096")
        advance.assert_called_once_with("INBOX", 7, 4096)

    def test_private_mixed_and_rebound_dns_are_rejected_before_connect(
        self,
    ) -> None:
        for addresses in (
            ("127.0.0.1",),
            ("169.254.169.254",),
            ("8.8.8.8", "10.0.0.1"),
            ("::1",),
        ):
            results = [(2, 1, 6, "", (item, 993)) for item in addresses]
            with (
                patch("socket.getaddrinfo", return_value=results),
                self.assertRaises(MailError),
            ):
                resolve_host("imap.example.org", 993, ())

    def test_operator_can_allow_only_a_specific_private_network(self) -> None:
        with patch(
            "socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("10.0.1.2", 993))],
        ):
            self.assertEqual(
                resolve_host("internal.example.org", 993, ("10.0.1.0/24",)),
                "10.0.1.2",
            )
            with self.assertRaises(MailError):
                resolve_host("internal.example.org", 993, ("10.0.2.0/24",))

    def test_imap_literals_are_bounded_before_allocation(self) -> None:
        client = object.__new__(SafeIMAP)
        with self.assertRaises(MailError):
            client.read(13 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
