"""Verifies the fixture IdP rejects unregistered and malformed redirects."""

from __future__ import annotations

import ssl
import tempfile
import unittest
from pathlib import Path

import httpx

from infrastructure.auth.tests import test_sso


class IdpRedirectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        class Fixture(test_sso.SsoIntegrationTest):
            origin = "https://app.example.invalid"

        cls.fixture = Fixture
        directory = tempfile.TemporaryDirectory(prefix="galadril-idp-")
        cls.addClassCleanup(directory.cleanup)
        Fixture.directory = directory
        files = test_sso.identity_files()
        for name, body in files.items():
            target = Path(directory.name) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        Fixture.start_idp()
        cls.addClassCleanup(Fixture.doClassCleanups)
        cls.context = ssl.create_default_context(
            cadata=files["identity/hydra/ca.pem"].decode()
        )
        cls.context.maximum_version = ssl.TLSVersion.TLSv1_2
        cls.endpoint = Fixture.idp_origin.replace(
            "host.docker.internal", "127.0.0.1"
        )

    def test_only_registered_callback_receives_authorization_code(self) -> None:
        callback = self.fixture.origin + "/api/oauth/oidc"
        with httpx.Client(verify=self.context) as client:
            response = client.get(
                self.endpoint + "/authorize",
                params={
                    "redirect_uri": callback,
                    "nonce": "nonce",
                    "state": "state\r\nX-Injected: value",
                },
            )
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        self.assertTrue(location.startswith(callback + "?code="))
        self.assertNotIn("\r", location)
        self.assertNotIn("\n", location)
        self.assertIn("%0D%0A", location)

    def test_invalid_authorization_requests_fail_without_location(self) -> None:
        callback = self.fixture.origin + "/api/oauth/oidc"
        with httpx.Client(verify=self.context) as client:
            for redirect in (
                "https://attacker.invalid/callback",
                callback + "\r\nX-Injected: value",
                callback + "?extra=value",
                "//attacker.invalid/callback",
                "",
            ):
                with self.subTest(redirect=redirect):
                    response = client.get(
                        self.endpoint + "/authorize",
                        params={
                            "redirect_uri": redirect,
                            "nonce": "nonce",
                            "state": "state",
                        },
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn("location", response.headers)
            response = client.get(self.endpoint + "/authorize")
            self.assertEqual(response.status_code, 400)
            for missing in ("nonce", "state"):
                params = {
                    "redirect_uri": callback,
                    "nonce": "nonce",
                    "state": "state",
                }
                del params[missing]
                response = client.get(
                    self.endpoint + "/authorize", params=params
                )
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
