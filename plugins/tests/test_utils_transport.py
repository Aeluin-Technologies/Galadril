"""Provider boundaries reject secret redirection and unbounded remote payloads."""

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
