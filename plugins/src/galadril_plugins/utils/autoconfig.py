"""Read bounded public mail settings over certificate-verified, pinned HTTPS."""

from __future__ import annotations

import asyncio
import http.client
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Literal
from urllib.parse import quote

import structlog
from galadril_plugins.utils.models import Contract
from galadril_plugins.utils.store import MailError
from galadril_plugins.utils.transport import resolve_host
from opentelemetry import trace

MAX_CONFIG_BYTES = 65536
logger = structlog.get_logger(__name__)


class PublishedConfig(Contract):
    imap_host: str
    smtp_host: str
    smtp_port: Literal[465, 587] = 465
    username: Literal["email", "local_part", "manual"] = "email"
    oauth_provider: Literal["google", "microsoft"] | None = None


def oauth_provider_for(
    imap: str, smtp: str
) -> Literal["google", "microsoft"] | None:
    """Recognize only fixed provider endpoints supported by the consent adapters."""
    if imap == "imap.gmail.com" and smtp == "smtp.gmail.com":
        return "google"
    if imap == "outlook.office365.com" and smtp in {
        "smtp.office365.com",
        "smtp-mail.outlook.com",
    }:
        return "microsoft"
    return None


class NoDocumentTypes(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("Mail configuration cannot declare document types")


def parse_configuration(document: bytes, domain: str) -> PublishedConfig | None:
    """Reject ambiguous providers, non-TLS endpoints and XML entity declarations."""
    from galadril_plugins.utils.discovery import normalize_domain

    if len(document) > MAX_CONFIG_BYTES:
        return None
    try:
        root = ET.fromstring(
            document, parser=ET.XMLParser(target=NoDocumentTypes())
        )
        providers = root.findall("emailProvider")
        provider = next(
            (
                item
                for item in providers
                if domain
                in {
                    (entry.text or "").strip().lower()
                    for entry in item.findall("domain")
                }
            ),
            None,
        )
        if provider is None:
            return None
        incoming = next(
            (
                item
                for item in provider.findall("incomingServer")
                if item.get("type") == "imap"
                and item.findtext("port") == "993"
                and item.findtext("socketType") == "SSL"
            ),
            None,
        )
        outgoing = next(
            (
                item
                for item in provider.findall("outgoingServer")
                if item.get("type") == "smtp"
                and (
                    item.findtext("port") == "465"
                    and item.findtext("socketType") == "SSL"
                    or item.findtext("port") == "587"
                    and item.findtext("socketType") == "STARTTLS"
                )
            ),
            None,
        )
        if incoming is None or outgoing is None:
            return None
        imap = normalize_domain(incoming.findtext("hostname", ""))
        smtp = normalize_domain(outgoing.findtext("hostname", ""))
        username = incoming.findtext("username", "").strip()
        oauth: Literal["google", "microsoft"] | None = None
        if "OAuth2" in [
            entry.text for entry in incoming.findall("authentication")
        ]:
            oauth = oauth_provider_for(imap, smtp)
        return PublishedConfig(
            imap_host=imap,
            smtp_host=smtp,
            smtp_port=587 if outgoing.findtext("port") == "587" else 465,
            username="email"
            if username == "%EMAILADDRESS%"
            else "local_part"
            if username == "%EMAILLOCALPART%"
            else "manual",
            oauth_provider=oauth,
        )
    except (ET.ParseError, ValueError):
        return None


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self) -> None:
        pinned = resolve_host(self.host, 443, ())
        stream = socket.create_connection((pinned, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(
                stream, server_hostname=self.host
            )
        except BaseException:
            stream.close()
            raise


def fetch_configuration(host: str, path: str) -> bytes | None:
    connection = PinnedHTTPS(host, timeout=2)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/xml",
                "Accept-Encoding": "identity",
            },
        )
        response = connection.getresponse()
        if (
            response.status != 200
            or response.getheader("Content-Encoding", "identity") != "identity"
        ):
            return None
        data = response.read(MAX_CONFIG_BYTES + 1)
        return data if len(data) <= MAX_CONFIG_BYTES else None
    except (OSError, http.client.HTTPException, MailError, ValueError):
        logger.debug("utils.discovery.public_configuration_unavailable")
        return None
    finally:
        connection.close()


class AutoConfiguration:
    __slots__ = ("_fetch", "_cache")

    def __init__(
        self, fetch: Callable[[str, str], bytes | None] = fetch_configuration
    ) -> None:
        self._fetch = fetch
        self._cache: dict[str, tuple[float, PublishedConfig]] = {}

    async def lookup(self, domain: str) -> PublishedConfig | None:
        from galadril_plugins.utils.discovery import normalize_domain

        domain = normalize_domain(domain)
        cached = self._cache.get(domain)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        # A synthetic local part selects domain-specific templates without disclosing a mailbox.
        query = "?emailaddress=" + quote("discovery@" + domain, safe="")
        candidates = [
            ("autoconfig.thunderbird.net", "/v1.1/" + domain),
            ("autoconfig." + domain, "/mail/config-v1.1.xml" + query),
            (domain, "/.well-known/autoconfig/mail/config-v1.1.xml" + query),
        ]
        if len(domain.split(".")) >= 3:
            candidates.append(
                (
                    "autoconfig." + domain.partition(".")[2],
                    "/mail/config-v1.1.xml" + query,
                )
            )
        with trace.get_tracer("galadril.plugins.utils").start_as_current_span(
            "utils.email.autoconfigure"
        ):
            for host, path in candidates:
                body = await asyncio.to_thread(self._fetch, host, path)
                config = (
                    parse_configuration(body, domain)
                    if body is not None
                    else None
                )
                if config is not None:
                    if len(self._cache) >= 128:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[domain] = (time.monotonic() + 300, config)
                    return config
        return None
