"""TLS-only mail adapters pin resolved addresses before authenticating."""

from __future__ import annotations

import imaplib
import ipaddress
import smtplib
import socket
import ssl
from collections.abc import Callable

from galadril_plugins.utils.mime import MAX_MESSAGE_BYTES, parse_message
from galadril_plugins.utils.models import (
    ConnectionInput,
    Credentials,
    MailMessage,
)
from galadril_plugins.utils.store import MailError


def validate_addresses(
    addresses: tuple[str, ...], allowed_networks: tuple[str, ...]
) -> None:
    networks = tuple(ipaddress.ip_network(value) for value in allowed_networks)
    if not addresses or len(addresses) > 32:
        raise MailError("Mail server address unavailable")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global and not any(
            address in network for network in networks
        ):
            raise MailError("Mail server network is not allowed")


def resolve_host(
    host: str, port: int, allowed_networks: tuple[str, ...]
) -> str:
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))
    validate_addresses(addresses, allowed_networks)
    return addresses[0]


class SafeIMAP(imaplib.IMAP4_SSL):
    def __init__(
        self, host: str, port: int, pinned: str, *, timeout: float = 15
    ) -> None:
        self.pinned = pinned
        self._tls_context = ssl.create_default_context()
        super().__init__(
            host, port, ssl_context=self._tls_context, timeout=timeout
        )

    def _create_socket(self, timeout: float | None) -> ssl.SSLSocket:
        stream = socket.create_connection((self.pinned, self.port), timeout)
        try:
            return self._tls_context.wrap_socket(
                stream, server_hostname=self.host
            )
        except BaseException:
            stream.close()
            raise

    def read(self, size: int) -> bytes:
        if size > MAX_MESSAGE_BYTES:
            raise MailError("IMAP literal exceeds size limit")
        return super().read(size)


class SafeSMTP(smtplib.SMTP):
    def __init__(
        self, host: str, port: int, pinned: str, *, timeout: float = 15
    ) -> None:
        self.pinned, self.implicit_tls = pinned, port == 465
        self.context = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout)
        if not self.implicit_tls:
            self.starttls(context=self.context)
        self.ehlo()

    def _get_socket(
        self, host: str, port: int, timeout: float | None
    ) -> socket.socket:
        stream = socket.create_connection((self.pinned, port), timeout)
        if not self.implicit_tls:
            return stream
        try:
            return self.context.wrap_socket(stream, server_hostname=host)
        except BaseException:
            stream.close()
            raise


def _login(client: SafeIMAP, credentials: Credentials) -> None:
    """Translate authentication errors without retaining provider responses."""
    try:
        if credentials.auth == "password":
            client.login(
                credentials.username, credentials.password.get_secret_value()
            )
        else:
            response = f"user={credentials.username}\x01auth=Bearer {credentials.password.get_secret_value()}\x01\x01".encode()
            client.authenticate(
                "XOAUTH2", lambda challenge: response if not challenge else b""
            )
    except imaplib.IMAP4.abort:
        raise MailError("CONNECTION_UNAVAILABLE") from None
    except imaplib.IMAP4.error:
        raise MailError("AUTHENTICATION_FAILED") from None


class MailTransport:
    __slots__ = ("_networks",)

    def __init__(self, allowed_networks: tuple[str, ...] = ()) -> None:
        self._networks = allowed_networks

    @property
    def allowed_networks(self) -> tuple[str, ...]:
        return self._networks

    def validate_servers(
        self, imap_host: str, smtp_host: str, smtp_port: int
    ) -> None:
        """Verify pinned TLS handshakes without sending credentials or mail."""
        with SafeIMAP(
            imap_host,
            993,
            resolve_host(imap_host, 993, self._networks),
            timeout=5,
        ):
            pass
        with SafeSMTP(
            smtp_host,
            smtp_port,
            resolve_host(smtp_host, smtp_port, self._networks),
            timeout=5,
        ):
            pass

    def test(self, config: ConnectionInput, credentials: Credentials) -> None:
        with SafeIMAP(
            config.imap_host,
            config.imap_port,
            resolve_host(config.imap_host, config.imap_port, self._networks),
        ) as client:
            _login(client, credentials)
            if client.noop()[0] != "OK":
                raise MailError("Mail connection rejected")

    def sync(
        self,
        config: ConnectionInput,
        credentials: Credentials,
        cursor: Callable[[str], tuple[int | None, int]],
        consume: Callable[[str, int, int, MailMessage], None],
        advance: Callable[[str, int, int], None],
    ) -> int:
        processed = 0
        with SafeIMAP(
            config.imap_host,
            config.imap_port,
            resolve_host(config.imap_host, config.imap_port, self._networks),
        ) as client:
            _login(client, credentials)
            for folder in config.folders:
                status, _ = client.select('"' + folder + '"', readonly=True)
                if status != "OK":
                    raise MailError("Selected mail folder unavailable")
                validity_values = client.response("UIDVALIDITY")[1]
                next_values = client.response("UIDNEXT")[1]
                if (
                    not validity_values
                    or not validity_values[0]
                    or not next_values
                    or not next_values[0]
                ):
                    raise MailError(
                        "IMAP server did not provide durable UID identity"
                    )
                validity, next_uid = (
                    int(validity_values[0]),
                    int(next_values[0]),
                )
                old_validity, old_uid = cursor(folder)
                if old_validity is None and config.initial_sync == "new":
                    advance(folder, validity, max(0, next_uid - 1))
                    continue
                start = old_uid + 1 if old_validity == validity else 1
                end = min(start + 4095, next_uid - 1)
                if start > end:
                    continue
                status, results = client.uid("search", "UID", f"{start}:{end}")
                if status != "OK" or not results or results[0] is None:
                    raise MailError("IMAP search failed")
                uids = results[0].split()
                if len(uids) > 4096:
                    raise MailError("IMAP response exceeds batch limit")
                numbers = sorted({int(uid) for uid in uids})
                if len(numbers) != len(uids) or any(
                    not start <= number <= end for number in numbers
                ):
                    raise MailError("IMAP UID escaped requested range")
                batch = numbers[:25]
                for number in batch:
                    status, parts = client.uid(
                        "fetch", str(number), "(RFC822.SIZE BODY.PEEK[])"
                    )
                    if status != "OK":
                        raise MailError("IMAP message fetch failed")
                    raw: bytes | None = None
                    for part in parts:
                        if (
                            isinstance(part, tuple)
                            and len(part) == 2
                            and isinstance(part[1], bytes)
                        ):
                            if raw is not None:
                                raise MailError("Ambiguous IMAP message")
                            raw = part[1]
                    if raw is None:
                        raise MailError("IMAP message unavailable")
                    consume(folder, validity, number, parse_message(raw))
                    processed += 1
                advance(
                    folder, validity, batch[-1] if len(numbers) > 25 else end
                )
        return processed

    def send(
        self,
        config: ConnectionInput,
        credentials: Credentials,
        recipients: tuple[str, ...],
        raw: bytes,
    ) -> None:
        with SafeSMTP(
            config.smtp_host,
            config.smtp_port,
            resolve_host(config.smtp_host, config.smtp_port, self._networks),
        ) as client:
            if credentials.auth == "password":
                client.login(
                    credentials.username,
                    credentials.password.get_secret_value(),
                )
            else:
                token = f"user={credentials.username}\x01auth=Bearer {credentials.password.get_secret_value()}\x01\x01"
                client.auth(
                    "XOAUTH2",
                    lambda challenge=None: token if challenge is None else "",
                )
            refused = client.sendmail(config.address, recipients, raw)
            if refused:
                raise MailError(
                    "Some recipients were rejected; reconcile delivery"
                )
