"""DNS discovery never acquires credentials or relaxes connection policy."""

import asyncio
import socketserver
import threading
import unittest

import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset
from galadril_plugins.utils.discovery import (
    Discovery,
    DiscoveryError,
    DNSResolver,
    ServiceRecord,
    normalize_domain,
)


class AuthoritativeDNS(socketserver.UDPServer):
    def __init__(self) -> None:
        self.queries: list[tuple[str, int]] = []
        super().__init__(("127.0.0.1", 0), DNSHandler)


class DNSHandler(socketserver.DatagramRequestHandler):
    def handle(self) -> None:
        if not isinstance(self.server, AuthoritativeDNS):
            return
        query = dns.message.from_wire(self.rfile.read(4096))
        response = dns.message.make_response(query)
        question = query.question[0]
        name = question.name.to_text()
        self.server.queries.append((name, question.rdtype))
        records = {
            "_imaps._tcp.example.org.": "0 1 993 imap.provider.org.",
            "_submissions._tcp.example.org.": "0 1 465 smtp.provider.org.",
            "_submission._tcp.example.org.": "0 1 587 smtp.provider.org.",
        }
        if question.rdtype == dns.rdatatype.SRV and name in records:
            response.answer.append(
                dns.rrset.from_text(name, 60, "IN", "SRV", records[name])
            )
        elif question.rdtype == dns.rdatatype.A and name in {
            "imap.provider.org.",
            "smtp.provider.org.",
        }:
            response.answer.append(
                dns.rrset.from_text(name, 60, "IN", "A", "8.8.8.8")
            )
        elif name == "broken.example.org.":
            response.set_rcode(dns.rcode.SERVFAIL)
        self.wfile.write(response.to_wire())


class Resolver:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.records: dict[str, tuple[ServiceRecord, ...]] = {}
        self.hosts: dict[str, tuple[str, ...]] = {}
        self.failed: set[str] = set()

    async def services(self, name: str) -> tuple[ServiceRecord, ...]:
        self.queries.append(name)
        if name in self.failed:
            raise DiscoveryError("DNS unavailable")
        return self.records.get(name, ())

    async def addresses(self, name: str) -> tuple[str, ...]:
        self.queries.append(name)
        return self.hosts.get(name, ("8.8.8.8",))


class MailDiscovery(unittest.IsolatedAsyncioTestCase):
    async def test_real_dns_packets_are_absolute_and_srv_answers_are_ttl_cached(
        self,
    ) -> None:
        server = AuthoritativeDNS()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            resolver = DNSResolver()
            resolver._resolver.nameservers = ["127.0.0.1"]
            resolver._resolver.port = server.server_address[1]
            discovery = Discovery(resolver)
            result = await discovery.discover("user@example.org")
            self.assertEqual(result.imap[0].host, "imap.provider.org")
            self.assertEqual([item.port for item in result.smtp], [465, 587])
            await discovery.discover("example.org")
            self.assertEqual(
                server.queries.count(
                    ("_imaps._tcp.example.org.", dns.rdatatype.SRV)
                ),
                1,
            )
            self.assertFalse(
                any(kind == dns.rdatatype.MX for name, kind in server.queries)
            )
            self.assertTrue(
                all(
                    name.endswith(".") and "user" not in name
                    for name, kind in server.queries
                )
            )
            with self.assertRaises(DiscoveryError):
                await resolver.services("broken.example.org.")
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)

    def setUp(self) -> None:
        self.resolver = Resolver()

    def test_only_a_canonical_domain_enters_dns(self) -> None:
        self.assertEqual(
            normalize_domain(" Person@EXAMPLE.org. "), "example.org"
        )
        self.assertEqual(
            normalize_domain("bücher.example"), "xn--bcher-kva.example"
        )
        for value in (
            "https://example.org",
            "localhost",
            "127.0.0.1",
            "a..org",
            "-a.org",
            "x.local",
            "x.home.arpa",
            "x\r\n.org",
            "@example.org",
            "a@b@example.org",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_domain(value)

    async def test_srv_priority_tls_and_cross_domain_delegation(self) -> None:
        self.resolver.records = {
            "_imaps._tcp.example.org.": (
                ServiceRecord(
                    host="backup.provider.org", port=993, priority=20, weight=1
                ),
                ServiceRecord(
                    host="primary.provider.org", port=993, priority=0, weight=2
                ),
                ServiceRecord(
                    host="primary.provider.org", port=993, priority=30, weight=1
                ),
            ),
            "_submissions._tcp.example.org.": (
                ServiceRecord(host="smtp.provider.org", port=465),
            ),
            "_submission._tcp.example.org.": (
                ServiceRecord(host="smtp.provider.org", port=587),
            ),
        }
        result = await Discovery(self.resolver).discover("user@EXAMPLE.org")
        self.assertEqual(result.domain, "example.org")
        self.assertEqual(
            [item.host for item in result.imap],
            ["primary.provider.org", "backup.provider.org"],
        )
        self.assertEqual([item.port for item in result.smtp], [465, 587])
        self.assertEqual(
            [item.security for item in result.smtp], ["tls", "starttls"]
        )
        self.assertNotIn("user", " ".join(self.resolver.queries))
        self.assertFalse(
            any(
                "MX" in item or "http" in item for item in self.resolver.queries
            )
        )

    async def test_missing_unavailable_and_explicitly_disabled_are_distinct(
        self,
    ) -> None:
        self.resolver.records["_imaps._tcp.example.org."] = (
            ServiceRecord(host=".", port=0),
        )
        self.resolver.failed.add("_submissions._tcp.example.org.")
        result = await Discovery(self.resolver).discover("example.org")
        self.assertEqual(result.imap_status, "absent")
        self.assertEqual(result.smtp_status, "unavailable")
        self.assertFalse(result.imap or result.smtp)

    async def test_unsupported_ports_and_mixed_private_addresses_fail_closed(
        self,
    ) -> None:
        self.resolver.records = {
            "_imaps._tcp.example.org.": (
                ServiceRecord(host="imap.provider.org", port=143),
            ),
            "_submissions._tcp.example.org.": (
                ServiceRecord(host="smtp.provider.org", port=465),
            ),
        }
        self.resolver.hosts["smtp.provider.org"] = ("8.8.8.8", "127.0.0.1")
        result = await Discovery(self.resolver).discover("example.org")
        self.assertEqual(result.imap_status, "unsupported")
        self.assertEqual(result.smtp_status, "blocked")
        self.assertFalse(result.imap or result.smtp)

    async def test_private_destinations_require_operator_network_configuration(
        self,
    ) -> None:
        self.resolver.records["_imaps._tcp.example.org."] = (
            ServiceRecord(host="imap.provider.org", port=993),
        )
        self.resolver.hosts["imap.provider.org"] = ("10.0.1.2",)
        result = await Discovery(self.resolver, ("10.0.1.0/24",)).discover(
            "example.org"
        )
        self.assertEqual(len(result.imap), 1)

    async def test_srv_targets_are_hostnames_and_missing_addresses_are_unavailable(
        self,
    ) -> None:
        self.resolver.records["_imaps._tcp.example.org."] = (
            ServiceRecord(host="user@provider.org", port=993),
        )
        self.resolver.records["_submissions._tcp.example.org."] = (
            ServiceRecord(host="missing.provider.org", port=465),
        )
        self.resolver.hosts["missing.provider.org"] = ()
        result = await Discovery(self.resolver).discover("example.org")
        self.assertEqual(result.imap_status, "blocked")
        self.assertEqual(result.smtp_status, "unavailable")

    async def test_record_budget_is_bounded_before_address_queries(
        self,
    ) -> None:
        self.resolver.records["_imaps._tcp.example.org."] = tuple(
            ServiceRecord(host=f"mail{index}.provider.org", port=993)
            for index in range(33)
        )
        result = await Discovery(self.resolver).discover("example.org")
        self.assertEqual(result.imap_status, "unavailable")
        self.assertFalse(
            any(item.startswith("mail") for item in self.resolver.queries)
        )

    async def test_total_lookup_deadline_does_not_block_the_event_loop(
        self,
    ) -> None:
        class SlowResolver(Resolver):
            async def services(self, name: str) -> tuple[ServiceRecord, ...]:
                await asyncio.Event().wait()
                return ()

        with self.assertRaises(DiscoveryError):
            await Discovery(SlowResolver(), timeout=0.01).discover(
                "example.org"
            )


if __name__ == "__main__":
    unittest.main()
