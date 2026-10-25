"""Contact lookup keeps directory authority inside the authenticated mail service."""

import unittest

from galadril_plugins.utils.contacts import parse_contacts, provider_host
from galadril_plugins.utils.store import MailError


class Contacts(unittest.TestCase):
    def test_zimbra_formatted_addresses_resolve_without_accepting_header_injection(
        self,
    ) -> None:
        page = parse_contacts(
            b"""<AutoCompleteResponse xmlns="urn:zimbraMail"><match email='"Jean-Daniel Durand" &lt;jean-daniel@example.org&gt;' full="Jean-Daniel Durand"/><match email='"Bad&#10;Bcc: hidden@example.org" &lt;bad@example.org&gt;'/></AutoCompleteResponse>"""
        )
        self.assertEqual(
            [(item.name, item.email) for item in page.items],
            [("Jean-Daniel Durand", "jean-daniel@example.org")],
        )

    def test_only_explicit_providers_receive_the_account_credential(
        self,
    ) -> None:
        self.assertEqual(
            provider_host("person@etu.univ-lorraine.fr", {}),
            "mail.etu.univ-lorraine.fr",
        )
        self.assertEqual(
            provider_host("person@univ-lorraine.fr", {}),
            "mail.univ-lorraine.fr",
        )
        self.assertIsNone(
            provider_host("person@etu.univ-lorraine.fr.attacker.org", {})
        )
        self.assertIsNone(provider_host("person@example.org", {}))
        self.assertEqual(
            provider_host(
                "person@example.org", {"example.org": "mail.example.org"}
            ),
            "mail.example.org",
        )

    def test_directory_results_are_bounded_validated_and_deduplicated(
        self,
    ) -> None:
        page = parse_contacts(
            b"""<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body><AutoCompleteResponse xmlns="urn:zimbraMail"><match email="jean@example.org" display="Jean-Daniel Durand" type="gal"/><match email="JEAN@example.org" display="Duplicate" type="contact"/><match email="group@example.org" isGroup="1"/><match email="bad&#10;@example.org"/><match email="other@example.org" display="Jean-Daniel Martin" type="contact"/></AutoCompleteResponse></soap:Body></soap:Envelope>"""
        )
        self.assertEqual(
            [entry.email for entry in page.items],
            ["jean@example.org", "other@example.org"],
        )
        self.assertEqual(page.items[0].name, "Jean-Daniel Durand")
        self.assertFalse(page.more)
        full = (
            b'<AutoCompleteResponse xmlns="urn:zimbraMail">'
            + b'<match email="a@example.org"/>' * 20
            + b"</AutoCompleteResponse>"
        )
        self.assertTrue(parse_contacts(full).more)

    def test_untrusted_xml_never_expands_entities_or_leaks_provider_faults(
        self,
    ) -> None:
        for body in (
            b'<!DOCTYPE x [<!ENTITY x "private">]><AutoCompleteResponse>&x;</AutoCompleteResponse>',
            b"<Fault><Text>private provider diagnostic</Text></Fault>",
            b"x" * (512 * 1024 + 1),
        ):
            with self.assertRaises(MailError) as raised:
                parse_contacts(body)
            self.assertNotIn("private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
