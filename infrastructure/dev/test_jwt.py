"""Checks local signing material and the public proxy's JWT claim contract."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import jwt

from infrastructure.dev.galadril_dev.jwt import initialize, issue_token


class LocalJwtTest(unittest.TestCase):
    def test_generated_jwks_verifies_a_short_lived_tenant_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "jwt"
            initialize(directory)
            jwks = jwt.PyJWKSet.from_json((directory / "jwks.json").read_text())
            token = issue_token(directory, "local-user", "local-tenant")
            key = jwks.keys[0]
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["ES256"],
                issuer="https://aeluin.gravitalia.com",
                audience="galadril",
                options={"require": ["exp", "iat", "nbf", "sub", "tenant_id"]},
            )
            self.assertEqual(claims["sub"], "local-user")
            self.assertEqual(claims["tenant_id"], "local-tenant")
            self.assertEqual(claims["exp"] - claims["iat"], 900)
            self.assertLessEqual(claims["iat"], int(time.time()))
            self.assertEqual(
                jwt.get_unverified_header(token)["kid"], key.key_id
            )
            public = json.loads((directory / "jwks.json").read_text())
            self.assertNotIn("d", public["keys"][0])
            self.assertEqual((directory.stat().st_mode & 0o777), 0o700)
            self.assertEqual(
                (directory / "signing-key.pem").stat().st_mode & 0o777, 0o600
            )

    def test_initialization_preserves_existing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "jwt"
            initialize(directory)
            original = (directory / "signing-key.pem").read_bytes()
            with self.assertRaises(FileExistsError):
                initialize(directory)
            self.assertEqual(
                (directory / "signing-key.pem").read_bytes(), original
            )

    def test_token_requires_subject_and_tenant(self) -> None:
        for subject, tenant in (("", "tenant"), ("user", "")):
            with self.subTest(subject=subject, tenant=tenant):
                with self.assertRaises(ValueError):
                    issue_token(Path("missing"), subject, tenant)


if __name__ == "__main__":
    unittest.main()
