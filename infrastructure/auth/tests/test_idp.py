"""Verifies the fixture IdP rejects unregistered and malformed redirects."""

from __future__ import annotations

import ssl
import tempfile
import traceback
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from infrastructure.auth.tests import test_sso


@dataclass(frozen=True, slots=True)
class ProbeResponse:
    exit_code: int
    output: bytes


class NativeIdentityProbeTest(unittest.TestCase):
    def probe(self, response: ProbeResponse) -> int:
        def execute(command: list[str]) -> ProbeResponse:
            self.assertEqual(
                command[-1], "__Host-galadril-identity=private-cookie"
            )
            return response

        case = test_sso.SsoIntegrationTest()
        with patch.object(
            test_sso.SsoIntegrationTest,
            "studio",
            SimpleNamespace(exec_run=execute),
            create=True,
        ):
            return case.identity_session_status("private-cookie")

    def test_native_http_status_is_preserved_without_retries(self) -> None:
        for status in (200, 401, 403, 503):
            self.assertEqual(
                self.probe(ProbeResponse(0, str(status).encode())), status
            )

    def test_transport_failure_reports_only_safe_diagnostics(self) -> None:
        with self.assertRaisesRegex(
            AssertionError, "TimeoutError.*UND_ERR_CONNECT_TIMEOUT"
        ) as failure:
            self.probe(
                ProbeResponse(
                    1,
                    b'{"error":"TimeoutError","code":"UND_ERR_CONNECT_TIMEOUT"}',
                )
            )
        self.assertNotIn("private-cookie", str(failure.exception))

    def test_unstructured_failure_and_invalid_status_do_not_leak_output(
        self,
    ) -> None:
        for response in (
            ProbeResponse(1, b"private-provider-body"),
            ProbeResponse(0, b"private-provider-body"),
            ProbeResponse(0, b"0"),
        ):
            with self.assertRaises(AssertionError) as failure:
                self.probe(response)
            self.assertNotIn("private-provider-body", str(failure.exception))
            self.assertNotIn(
                "private-provider-body",
                "".join(traceback.format_exception(failure.exception)),
            )


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
