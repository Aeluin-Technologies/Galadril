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
    def test_blank_imap_is_an_authenticated_sending_only_connection(
        self,
    ) -> None:
        config = ConnectionInput(
            name="Relay",
            address="me@example.org",
            smtp_host="mail-eu.smtp2go.com",
            smtp_port=2525,
        )
        credentials = Credentials(
            username="smtp-user", password=SecretStr("private")
        )
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.sendmail.return_value = {}
        smtp.noop.return_value = (250, b"OK")
        with (
            patch("galadril_plugins.utils.transport.SafeIMAP") as incoming,
            patch(
                "galadril_plugins.utils.transport.SafeSMTP", return_value=smtp
            ),
            patch(
                "galadril_plugins.utils.transport.resolve_host",
                return_value="8.8.8.8",
            ),
        ):
            transport = MailTransport()
            transport.validate_servers("", config.smtp_host, config.smtp_port)
            transport.test(config, credentials)
            transport.send(
                config, credentials, ("recipient@example.org",), b"Test"
            )
            self.assertEqual(
                transport.sync(
                    config, credentials, MagicMock(), MagicMock(), MagicMock()
                ),
                0,
            )
        incoming.assert_not_called()
        self.assertEqual(smtp.login.call_count, 2)
        smtp.sendmail.assert_called_once_with(
            "me@example.org", ("recipient@example.org",), b"Test"
        )

    def test_smtp2go_ports_require_tls_before_authentication(self) -> None:
        from galadril_plugins.utils.transport import SafeSMTP

        for port in (25, 80, 443, 465, 587, 2525, 8025, 8465):
            with (
                self.subTest(port=port),
                patch("smtplib.SMTP.__init__", return_value=None),
                patch.object(SafeSMTP, "starttls") as starttls,
                patch.object(SafeSMTP, "ehlo"),
            ):
                client = SafeSMTP("mail-eu.smtp2go.com", port, "8.8.8.8")
                self.assertTrue(client.context.check_hostname)
                self.assertEqual(client.implicit_tls, port in (443, 465, 8465))
                self.assertEqual(
                    starttls.call_count, 0 if client.implicit_tls else 1
                )
        with self.assertRaises(ValueError):
            ConnectionInput(
                name="Relay",
                address="me@example.org",
                smtp_host="mail-eu.smtp2go.com",
                smtp_port=2525,
                ingest=True,
            )

    def test_failed_starttls_closes_the_socket_without_authentication(
        self,
    ) -> None:
        import smtplib

        from galadril_plugins.utils.transport import SafeSMTP

        with (
            patch("smtplib.SMTP.__init__", return_value=None),
            patch.object(
                SafeSMTP,
                "starttls",
                side_effect=smtplib.SMTPNotSupportedError("No TLS"),
            ),
            patch.object(SafeSMTP, "close") as close,
            patch.object(SafeSMTP, "login") as login,
        ):
            with self.assertRaises(smtplib.SMTPNotSupportedError):
                SafeSMTP("mail-eu.smtp2go.com", 2525, "8.8.8.8")
        close.assert_called_once()
        login.assert_not_called()

    def test_markdown_signature_preserves_lines_small_text_and_safe_images(
        self,
    ) -> None:
        from email import policy
        from email.parser import BytesParser

        from galadril_plugins.utils.mime import compose_reply
        from galadril_plugins.utils.models import DraftInput

        signature = "Bien cordialement,\n**Anthony CHENOT--NOGUES**\n\n<small>Ce message peut faire l'objet d'un traitement automatisé.</small>\n\n![Logo](https://static.example.org/logo.png)"
        message = BytesParser(policy=policy.default).parsebytes(
            compose_reply(
                "me@example.org",
                DraftInput(
                    to=("to@example.org",), subject="Test", body="Reply"
                ),
                "<signature@example.org>",
                signature_html=signature,
            )
        )
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("<strong>Anthony CHENOT--NOGUES</strong>", html)
        self.assertIn("Bien cordialement,<br>", html)
        self.assertIn("<small>Ce message", html)
        self.assertIn('src="https://static.example.org/logo.png"', html)
        self.assertIn('alt="Logo"', html)
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Anthony CHENOT--NOGUES", plain)
        self.assertNotIn("**Anthony", plain)
        self.assertIn("Logo", plain)

    def test_signature_html_line_breaks_and_template_values_are_not_markdown_injections(
        self,
    ) -> None:
        from email import policy
        from email.parser import BytesParser

        from galadril_plugins.utils.mime import compose_reply
        from galadril_plugins.utils.models import DraftInput

        value = DraftInput(
            to=("to@example.org",),
            subject="![Injected](https://unsafe.example.org/pixel)",
            body="Reply",
        )
        signature = "<div>Line one\nLine two</div><small>{{ subject }}</small><tiny>Normal text</tiny>"
        html = (
            BytesParser(policy=policy.default)
            .parsebytes(
                compose_reply(
                    "me@example.org",
                    value,
                    "<signature@example.org>",
                    signature_html=signature,
                )
            )
            .get_body(preferencelist=("html",))
            .get_content()
        )
        self.assertIn("Line one<br>Line two", html)
        self.assertIn("![Injected]", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<tiny", html)
        self.assertIn("Normal text", html)

    def test_signature_images_reject_local_and_credential_bearing_sources(
        self,
    ) -> None:
        from galadril_plugins.utils.signature import image_source

        for source in (
            "javascript:alert(1)",
            "data:image/svg+xml,<svg/>",
            "http://example.org/logo.png",
            "https://localhost/x",
            "https://127.0.0.1/x",
            "https://[::1]/x",
            "https://internal.local/x",
            "https://user:secret@example.org/x",
            "https://example.org:8443/x",
        ):
            with self.subTest(source=source):
                self.assertIsNone(image_source(source))
        self.assertEqual(
            image_source("https://static.example.org/logo.png"),
            "https://static.example.org/logo.png",
        )

    def test_existing_html_breaks_do_not_double_after_markdown_rendering(
        self,
    ) -> None:
        from galadril_plugins.utils.signature import render_signature

        html, plain = render_signature("One<br>\nTwo", {})
        self.assertEqual(html.count("<br>"), 1)
        self.assertEqual(plain, "One\nTwo")

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
            signature_html='<b>Regards</b><img src="http://127.0.0.1/pixel"><script>bad()</script>',
        )
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertIsNone(message.get("Bcc"))
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("<b>Regards</b>", html)
        self.assertIn("&lt;plain text&gt;", html)
        self.assertNotIn("127.0.0.1", html)
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
