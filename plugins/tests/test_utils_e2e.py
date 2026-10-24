"""Real TLS IMAP and SMTP exercise collection, approval and one-shot delivery."""

from __future__ import annotations

import asyncio
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from galadril_plugins.utils.models import (
    ConnectionInput,
    Credentials,
    Principal,
    ResponseProfile,
)
from galadril_plugins.utils.service import MailService, Operation
from galadril_plugins.utils.store import MailError, MailStore
from galadril_plugins.utils.transport import MailTransport
from pydantic import SecretStr

RAW = b"From: alice@example.org\r\nTo: me@example.org\r\nDate: Fri, 23 Oct 2026 12:00:00 +0000\r\nMessage-ID: <question@example.org>\r\nSubject: Delivery date\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nWhen will this ship?\r\n"
DELIVERIES: list[bytes] = []


class TLSMailServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        handler: type[socketserver.BaseRequestHandler],
        context: ssl.SSLContext,
    ) -> None:
        self.context = context
        super().__init__(("127.0.0.1", 0), handler)

    def get_request(self) -> tuple[ssl.SSLSocket, tuple[str, int]]:
        stream, address = super().get_request()
        stream.settimeout(10)
        return self.context.wrap_socket(stream, server_side=True), address


class IMAPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.wfile.write(b"* OK ready\r\n")
        while line := self.rfile.readline(8192):
            parts = line.strip().split()
            if len(parts) < 2:
                return
            tag, operation = parts[:2]
            operation = operation.upper()
            if operation == b"CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1 UIDPLUS\r\n")
            elif operation == b"EXAMINE" or operation == b"SELECT":
                self.wfile.write(
                    b"* FLAGS (\\Seen)\r\n* 1 EXISTS\r\n* OK [UIDVALIDITY 77] stable\r\n* OK [UIDNEXT 2] next\r\n"
                )
            elif operation == b"UID" and parts[2].upper() == b"SEARCH":
                self.wfile.write(b"* SEARCH 1\r\n")
            elif operation == b"UID" and parts[2].upper() == b"FETCH":
                self.wfile.write(
                    b"* 1 FETCH (UID 1 RFC822.SIZE "
                    + str(len(RAW)).encode()
                    + b" BODY[] {"
                    + str(len(RAW)).encode()
                    + b"}\r\n"
                    + RAW
                    + b")\r\n"
                )
            elif operation == b"LOGOUT":
                self.wfile.write(b"* BYE closing\r\n" + tag + b" OK logout\r\n")
                return
            self.wfile.write(tag + b" OK done\r\n")


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.wfile.write(b"220 localhost ESMTP\r\n")
        while line := self.rfile.readline(8192):
            command = line.split(maxsplit=1)[0].upper()
            if command == b"EHLO":
                self.wfile.write(
                    b"250-localhost\r\n250-AUTH PLAIN LOGIN\r\n250 SIZE 12582912\r\n"
                )
            elif command == b"AUTH":
                self.wfile.write(b"235 authenticated\r\n")
            elif command in {b"MAIL", b"RCPT", b"RSET"}:
                self.wfile.write(b"250 accepted\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 send data\r\n")
                body = bytearray()
                while part := self.rfile.readline(65536):
                    if part == b".\r\n":
                        break
                    body.extend(part)
                DELIVERIES.append(bytes(body))
                self.wfile.write(b"250 queued\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"500 unknown\r\n")


def certificates(directory: Path) -> tuple[ssl.SSLContext, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file = directory / "cert.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file = directory / "key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context, cert_file


class MailEndToEnd(unittest.TestCase):
    def test_real_collection_draft_human_approval_and_single_smtp_delivery(
        self,
    ) -> None:
        DELIVERIES.clear()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            context, ca = certificates(directory)
            imap = TLSMailServer(IMAPHandler, context)
            smtp = TLSMailServer(SMTPHandler, context)
            threads = [
                threading.Thread(target=server.serve_forever, daemon=True)
                for server in (imap, smtp)
            ]
            for thread in threads:
                thread.start()
            original_connect = socket.create_connection
            original_context = ssl.create_default_context

            def connect(
                address: tuple[str, int],
                timeout: float | None = None,
                source_address: tuple[str, int] | None = None,
            ) -> socket.socket:
                host, port = address
                mapped = (
                    imap.server_address
                    if port == 993
                    else smtp.server_address
                    if port == 465
                    else (host, port)
                )
                return original_connect(mapped, timeout, source_address)

            async def run() -> None:
                actor = Principal(tenant="tenant", user="owner")
                store = MailStore(
                    directory / "mail.db", {"v1": b"k" * 32}, "v1"
                )
                config = ConnectionInput(
                    name="Test",
                    address="me@example.org",
                    imap_host="localhost",
                    smtp_host="localhost",
                    initial_sync="all",
                )
                identifier = store.create(
                    actor,
                    config,
                    Credentials(
                        username="me@example.org",
                        password=SecretStr("test-password"),
                    ),
                )["id"]
                assert isinstance(identifier, str)
                store.save_profile(
                    actor,
                    identifier,
                    ResponseProfile(
                        model="local", skill_ids=(), mode="approval"
                    ),
                )
                service = MailService(
                    store,
                    MailTransport(("127.0.0.0/8", "::1/128")),
                    "x" * 48,
                    "http://scribe",
                    {},
                )
                service.gateway = "http://gateway"

                def inference(request: httpx.Request) -> httpx.Response:
                    if request.url.path == "/internal/utils/authorize":
                        return httpx.Response(200, json={"active": True})
                    return httpx.Response(
                        200,
                        json={
                            "body": "We are checking your delivery date.",
                            "summary": "Asks for the delivery date",
                            "category": "support",
                            "model": "local",
                        },
                    )

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(inference)
                ) as client:
                    service._http = client
                    await service.operate(
                        actor, Operation(operation="sync", account=identifier)
                    )
                    self.assertEqual(len(store.messages(actor, identifier)), 1)
                    drafts = store.drafts(actor, identifier)
                    self.assertEqual(len(drafts), 1)
                    draft_id = drafts[0]["id"]
                    assert isinstance(draft_id, str)
                    with self.assertRaises(MailError):
                        await service.operate(
                            actor,
                            Operation(
                                operation="send",
                                account=identifier,
                                draft=draft_id,
                                revision=1,
                            ),
                        )
                    await service.operate(
                        actor,
                        Operation(
                            operation="approve",
                            account=identifier,
                            draft=draft_id,
                            revision=1,
                        ),
                    )
                    await service.operate(
                        actor,
                        Operation(
                            operation="send",
                            account=identifier,
                            draft=draft_id,
                            revision=1,
                        ),
                    )
                    self.assertEqual(len(DELIVERIES), 1)
                    self.assertIn(
                        b"In-Reply-To: <question@example.org>", DELIVERIES[0]
                    )
                    self.assertEqual(
                        store.drafts(actor, identifier)[0]["status"], "sent"
                    )
                    with self.assertRaises(MailError):
                        await service.operate(
                            actor,
                            Operation(
                                operation="send",
                                account=identifier,
                                draft=draft_id,
                                revision=1,
                            ),
                        )
                    await service.operate(
                        actor, Operation(operation="sync", account=identifier)
                    )
                    self.assertEqual(len(store.messages(actor, identifier)), 1)
                    self.assertEqual(len(DELIVERIES), 1)

            try:
                with (
                    patch("socket.create_connection", side_effect=connect),
                    patch(
                        "galadril_plugins.utils.transport.ssl.create_default_context",
                        side_effect=lambda: original_context(cafile=ca),
                    ),
                ):
                    asyncio.run(run())
            finally:
                for server in (imap, smtp):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
