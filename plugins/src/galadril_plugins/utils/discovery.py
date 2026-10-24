"""Bounded DNS hints never authenticate, mutate accounts, or infer access from MX."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Literal, Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver
from dns.rdtypes.IN.A import A
from dns.rdtypes.IN.AAAA import AAAA
from dns.rdtypes.IN.SRV import SRV
from galadril_plugins.utils.models import Contract
from galadril_plugins.utils.store import MailError
from galadril_plugins.utils.transport import validate_addresses
from pydantic import Field

type Status = Literal[
    "found", "absent", "unavailable", "unsupported", "blocked"
]


class DiscoveryError(MailError):
    """Contains only public discovery outcomes, never resolver exceptions."""


def normalize_domain(value: str) -> str:
    if len(value) > 1024 or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ValueError("Invalid mail domain")
    domain = value.strip()
    if "@" in domain:
        local, separator, domain = domain.partition("@")
        if not local or "@" in domain or not separator:
            raise ValueError("Invalid mail domain")
    try:
        domain = domain.removesuffix(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Invalid mail domain") from None
    if (
        len(domain) > 253
        or "." not in domain
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        )
        or domain.endswith((".local", ".localhost", ".internal", ".home.arpa"))
    ):
        raise ValueError("Invalid mail domain")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError("Mail discovery requires a domain")


class ServiceRecord(Contract):
    host: str = Field(min_length=1, max_length=254)
    port: int = Field(ge=0, le=65535)
    priority: int = Field(default=0, ge=0, le=65535)
    weight: int = Field(default=0, ge=0, le=65535)


class Endpoint(ServiceRecord):
    security: Literal["tls", "starttls"]


class DiscoveryResult(Contract):
    domain: str
    imap: tuple[Endpoint, ...]
    smtp: tuple[Endpoint, ...]
    imap_status: Status
    smtp_status: Status


class Resolver(Protocol):
    async def services(self, name: str) -> tuple[ServiceRecord, ...]: ...

    async def addresses(self, name: str) -> tuple[str, ...]: ...


class DNSResolver:
    __slots__ = ("_resolver",)

    def __init__(self) -> None:
        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.cache = dns.resolver.LRUCache(max_size=256)

    async def services(self, name: str) -> tuple[ServiceRecord, ...]:
        try:
            answer = await self._resolver.resolve(
                name, "SRV", search=False, lifetime=2
            )
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return ()
        except dns.exception.DNSException:
            raise DiscoveryError("Mail DNS is unavailable") from None
        if len(answer) > 32:
            raise DiscoveryError("Mail DNS exceeds discovery limits")
        records: list[ServiceRecord] = []
        for item in answer:
            if not isinstance(item, SRV):
                raise DiscoveryError("Unexpected mail DNS response")
            records.append(
                ServiceRecord(
                    host=item.target.to_text(),
                    port=item.port,
                    priority=item.priority,
                    weight=item.weight,
                )
            )
        return tuple(records)

    async def _addresses(self, name: str, kind: str) -> tuple[str, ...]:
        try:
            answer = await self._resolver.resolve(
                name + ".", kind, search=False, lifetime=2
            )
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return ()
        except dns.exception.DNSException:
            raise DiscoveryError("Mail server DNS is unavailable") from None
        if len(answer) > 32:
            raise DiscoveryError("Mail DNS exceeds discovery limits")
        addresses: list[str] = []
        for item in answer:
            if not isinstance(item, (A, AAAA)):
                raise DiscoveryError("Unexpected mail DNS response")
            addresses.append(item.address)
        return tuple(addresses)

    async def addresses(self, name: str) -> tuple[str, ...]:
        ipv4, ipv6 = await asyncio.gather(
            self._addresses(name, "A"), self._addresses(name, "AAAA")
        )
        return tuple(dict.fromkeys((*ipv4, *ipv6)))


class Discovery:
    __slots__ = ("_resolver", "_networks", "_timeout", "_semaphore")

    def __init__(
        self,
        resolver: Resolver,
        allowed_networks: tuple[str, ...] = (),
        *,
        timeout: float = 8,
    ) -> None:
        self._resolver, self._networks, self._timeout = (
            resolver,
            allowed_networks,
            timeout,
        )
        self._semaphore = asyncio.Semaphore(4)

    async def _endpoint(
        self, record: ServiceRecord, security: Literal["tls", "starttls"]
    ) -> tuple[Endpoint | None, Status]:
        if "@" in record.host:
            return None, "blocked"
        try:
            host = normalize_domain(record.host)
        except ValueError:
            return None, "blocked"
        try:
            async with self._semaphore:
                addresses = await self._resolver.addresses(host)
        except DiscoveryError:
            return None, "unavailable"
        if not addresses:
            return None, "unavailable"
        try:
            validate_addresses(addresses, self._networks)
        except (MailError, ValueError):
            return None, "blocked"
        return Endpoint(
            host=host,
            port=record.port,
            priority=record.priority,
            weight=record.weight,
            security=security,
        ), "found"

    async def _service(
        self, name: str, port: int, security: Literal["tls", "starttls"]
    ) -> tuple[tuple[Endpoint, ...], Status]:
        try:
            records = await self._resolver.services(name)
        except DiscoveryError:
            return (), "unavailable"
        if len(records) > 32:
            return (), "unavailable"
        if not records or any(item.host == "." for item in records):
            return (), "absent"
        supported = tuple(item for item in records if item.port == port)
        if not supported:
            return (), "unsupported"
        results = await asyncio.gather(
            *(self._endpoint(item, security) for item in supported)
        )
        endpoints = tuple(item for item, status in results if item is not None)
        if endpoints:
            unique: dict[tuple[str, int], Endpoint] = {}
            for item in sorted(
                endpoints,
                key=lambda item: (item.priority, -item.weight, item.host),
            ):
                unique.setdefault((item.host, item.port), item)
            return tuple(unique.values()), "found"
        return (), "unavailable" if any(
            status == "unavailable" for item, status in results
        ) else "blocked"

    async def discover(self, value: str) -> DiscoveryResult:
        domain = normalize_domain(value)
        try:
            async with asyncio.timeout(self._timeout):
                imap, implicit, starttls = await asyncio.gather(
                    self._service(f"_imaps._tcp.{domain}.", 993, "tls"),
                    self._service(f"_submissions._tcp.{domain}.", 465, "tls"),
                    self._service(
                        f"_submission._tcp.{domain}.", 587, "starttls"
                    ),
                )
        except TimeoutError:
            raise DiscoveryError("Mail discovery timed out") from None
        smtp = (*implicit[0], *starttls[0])
        smtp_status: Status = (
            "found"
            if smtp
            else next(
                (
                    status
                    for status in ("unavailable", "blocked", "unsupported")
                    if status in (implicit[1], starttls[1])
                ),
                "absent",
            )
        )
        return DiscoveryResult(
            domain=domain,
            imap=imap[0],
            smtp=smtp,
            imap_status=imap[1],
            smtp_status=smtp_status,
        )
