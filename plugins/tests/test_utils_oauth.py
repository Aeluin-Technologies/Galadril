"""Consent callbacks remain single-use and bound to their initiating identity."""

import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from galadril_plugins.utils.models import Principal
from galadril_plugins.utils.oauth import OAuthBroker, OAuthClient
from galadril_plugins.utils.store import MailError, MailStore


class ConsentSecurity(unittest.TestCase):
    def test_pkce_state_cannot_cross_identity_or_be_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MailStore(Path(directory) / "db", {"one": b"x" * 32}, "one")
            broker = OAuthBroker(
                store,
                {
                    "google": OAuthClient(
                        client_id="app",
                        client_secret="secret",
                        redirect_uri="https://galadril.example/settings/email",
                    )
                },
            )
            owner = Principal(tenant="t", user="owner")
            url = broker.start(owner, "google")
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["code_challenge_method"], ["S256"])
            state = query["state"][0]
            with self.assertRaises(MailError):
                broker.consume(Principal(tenant="t", user="other"), state)
            result = broker.consume(owner, state)
            self.assertEqual(result["provider"], "google")
            self.assertGreater(len(result["verifier"]), 42)
            with self.assertRaises(MailError):
                broker.consume(owner, state)


if __name__ == "__main__":
    unittest.main()
