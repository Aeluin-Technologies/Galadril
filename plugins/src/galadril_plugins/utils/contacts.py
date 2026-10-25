"""Bounded Zimbra address-book queries never expose account credentials to agents."""

from __future__ import annotations

import http.client
import json
import os
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from email.utils import parseaddr
from typing import Literal

import structlog
from galadril_plugins.utils.autoconfig import NoDocumentTypes, PinnedHTTPS
from galadril_plugins.utils.discovery import normalize_domain
from galadril_plugins.utils.models import (
    Contract,
    Credentials,
    validate_address,
)
from galadril_plugins.utils.store import MailError
from galadril_plugins.utils.transport import resolve_host
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import Field, TypeAdapter, ValidationError, field_validator

MAX_BYTES = 512 * 1024
SOAP = "http://www.w3.org/2003/05/soap-envelope"
ACCOUNT = "urn:zimbraAccount"
MAIL = "urn:zimbraMail"
CONTEXT = "urn:zimbra"
logger = structlog.get_logger(__name__)
_OFFICIAL = {
    "univ-lorraine.fr": "mail.univ-lorraine.fr",
    "etu.univ-lorraine.fr": "mail.etu.univ-lorraine.fr",
}


class Contact(Contract):
    name: str = Field(min_length=1, max_length=256)
    email: str = Field(max_length=320)
    source: Literal["zimbra"] = "zimbra"

    _email = field_validator("email")(validate_address)

    @field_validator("name")
    @classmethod
    def printable(cls, value: str) -> str:
        if not value.strip() or any(
            not character.isprintable() for character in value
        ):
            raise ValueError("Invalid contact name")
        return value.strip()


class ContactPage(Contract):
    items: tuple[Contact, ...] = Field(default=(), max_length=20)
    more: bool = False
    incomplete: bool = False


def provider_host(address: str, configured: Mapping[str, str]) -> str | None:
    """Credentials go only to maintained provider bindings or explicit deployment settings."""
    domain = address.rsplit("@", 1)[-1].lower()
    return configured.get(domain, _OFFICIAL.get(domain))


def _xml(document: bytes) -> ET.Element:
    if len(document) > MAX_BYTES:
        raise MailError("CONTACTS_UNAVAILABLE")
    try:
        return ET.fromstring(
            document, parser=ET.XMLParser(target=NoDocumentTypes())
        )
    except (ET.ParseError, ValueError):
        raise MailError("CONTACTS_UNAVAILABLE") from None


def _result(document: bytes, namespace: str, name: str) -> ET.Element:
    root = _xml(document)
    tag = f"{{{namespace}}}{name}Response"
    result = root if root.tag == tag else root.find(f".//{tag}")
    if result is None:
        raise MailError("CONTACTS_UNAVAILABLE")
    return result


def parse_contacts(document: bytes) -> ContactPage:
    """Distribution lists and malformed addresses must never become inferred individual recipients."""
    result = _result(document, MAIL, "AutoComplete")
    matches = result.findall(f"{{{MAIL}}}match")
    selected: dict[str, Contact] = {}
    for match in matches[:20]:
        if match.get("isGroup") in {"1", "true"}:
            continue
        raw_address = match.get("email", "").strip()
        if any(not character.isprintable() for character in raw_address):
            continue
        address_name, email = parseaddr(raw_address, strict=True)
        display = (
            match.get("full")
            or match.get("display")
            or " ".join(filter(None, (match.get("first"), match.get("last"))))
        )
        name = parseaddr(display)[0] if "<" in display else display
        try:
            contact = Contact(
                name=(name or address_name or email)[:256], email=email
            )
        except ValidationError:
            continue
        selected.setdefault(contact.email.casefold(), contact)
    return ContactPage(items=tuple(selected.values()), more=len(matches) >= 20)


class DirectoryHTTPS(PinnedHTTPS):
    def __init__(self, host: str, networks: tuple[str, ...]) -> None:
        super().__init__(host, timeout=5)
        self.networks = networks

    def connect(self) -> None:
        import socket

        pinned = resolve_host(self.host, 443, self.networks)
        stream = socket.create_connection((pinned, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(
                stream, server_hostname=self.host
            )
        except BaseException:
            stream.close()
            raise


def _call(
    connection: DirectoryHTTPS, operation: ET.Element, token: str = ""
) -> bytes:
    envelope = ET.Element(f"{{{SOAP}}}Envelope")
    header = ET.SubElement(envelope, f"{{{SOAP}}}Header")
    context = ET.SubElement(header, f"{{{CONTEXT}}}context")
    if token:
        ET.SubElement(context, f"{{{CONTEXT}}}authToken").text = token
    ET.SubElement(context, f"{{{CONTEXT}}}nosession")
    ET.SubElement(envelope, f"{{{SOAP}}}Body").append(operation)
    connection.request(
        "POST",
        "/service/soap",
        body=ET.tostring(envelope, encoding="utf-8"),
        headers={
            "Content-Type": "application/soap+xml; charset=utf-8",
            "Accept-Encoding": "identity",
        },
    )
    response = connection.getresponse()
    if (
        response.status != 200
        or response.getheader("Content-Encoding", "identity") != "identity"
    ):
        raise MailError("CONTACTS_UNAVAILABLE")
    document = response.read(MAX_BYTES + 1)
    if len(document) > MAX_BYTES:
        raise MailError("CONTACTS_UNAVAILABLE")
    return document


class ZimbraDirectory:
    __slots__ = ("providers", "networks")

    def __init__(
        self,
        networks: tuple[str, ...] = (),
        providers: Mapping[str, str] | None = None,
    ) -> None:
        self.networks = networks
        if providers is None:
            source = os.environ.get("UTILS_ZIMBRA_DIRECTORIES", "{}")
            if len(source) > 8192:
                raise MailError("Invalid contact provider configuration")
            try:
                providers = TypeAdapter(dict[str, str]).validate_python(
                    json.loads(source)
                )
            except (ValidationError, ValueError):
                raise MailError(
                    "Invalid contact provider configuration"
                ) from None
        if len(providers) > 32:
            raise MailError("Too many contact providers")
        self.providers = {
            normalize_domain(domain): normalize_domain(host)
            for domain, host in providers.items()
        }

    def supports(self, address: str) -> bool:
        return provider_host(address, self.providers) is not None

    def search(
        self, address: str, credentials: Credentials, query: str
    ) -> ContactPage:
        host = provider_host(address, self.providers)
        if host is None or credentials.auth != "password":
            raise MailError("CONTACTS_UNAVAILABLE")
        query = query.strip()
        if not 2 <= len(query) <= 128 or any(
            not character.isprintable() for character in query
        ):
            raise MailError("Invalid contact query")
        connection = DirectoryHTTPS(host, self.networks)
        token = ""
        with trace.get_tracer("galadril.utils").start_as_current_span(
            "utils.contacts.search",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                auth = ET.Element(
                    f"{{{ACCOUNT}}}AuthRequest", persistAuthTokenCookie="0"
                )
                ET.SubElement(
                    auth, f"{{{ACCOUNT}}}account", by="name"
                ).text = credentials.username
                ET.SubElement(
                    auth, f"{{{ACCOUNT}}}password"
                ).text = credentials.password.get_secret_value()
                result = _result(_call(connection, auth), ACCOUNT, "Auth")
                token = result.findtext(f"{{{ACCOUNT}}}authToken", "")
                if not token or len(token) > 8192:
                    raise MailError("CONTACTS_UNAVAILABLE")
                request = ET.Element(
                    f"{{{MAIL}}}AutoCompleteRequest",
                    includeGal="1",
                    t="account",
                )
                ET.SubElement(request, f"{{{MAIL}}}name").text = query
                return parse_contacts(_call(connection, request, token))
            except (OSError, ValueError, http.client.HTTPException, MailError):
                span.set_status(
                    Status(StatusCode.ERROR, "Contact lookup unavailable")
                )
                raise MailError("CONTACTS_UNAVAILABLE") from None
            finally:
                if token:
                    try:
                        _call(
                            connection,
                            ET.Element(f"{{{ACCOUNT}}}EndSessionRequest"),
                            token,
                        )
                    except (
                        OSError,
                        ValueError,
                        http.client.HTTPException,
                        MailError,
                    ):
                        logger.warning("utils.contacts.session_cleanup_failed")
                connection.close()
