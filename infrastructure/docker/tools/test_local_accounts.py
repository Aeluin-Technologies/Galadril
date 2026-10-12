"""Protects administrator-only references used by local account enrollment."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from infrastructure.docker.tools import local_accounts
from infrastructure.docker.tools.local_accounts import (
    bind_identity,
    identity_payload,
)


class LocalAccountTest(unittest.TestCase):
    def test_enrollment_requires_operator_key_before_any_identity_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SSO_PROVISIONING_KEY": ""}):
                with patch.object(
                    local_accounts, "_private_request", new_callable=AsyncMock
                ) as request:
                    with self.assertRaisesRegex(
                        ValueError, "SSO_PROVISIONING_KEY"
                    ):
                        asyncio.run(
                            local_accounts.enroll(
                                "test@example.com",
                                "https://localhost:8080",
                                Path(directory) / "ca.pem",
                                Path(directory),
                                True,
                            )
                        )
                    request.assert_not_awaited()

    def test_private_identity_failures_keep_the_http_status(self) -> None:
        process = AsyncMock()
        process.communicate.return_value = (b'{"status":404,"body":null}', b"")
        process.returncode = 0
        with patch.object(
            asyncio, "create_subprocess_exec", return_value=process
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                asyncio.run(
                    local_accounts._private_request(
                        "http://127.0.0.1:4434/admin/identities/missing",
                        None,
                        "GET",
                    )
                )

    def test_missing_identity_requires_explicit_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "account.json"
            original = '{"identity":"old","request_id":"request"}'
            state.write_text(original)
            with patch.object(
                local_accounts,
                "_private_request",
                side_effect=local_accounts.IdentityApiError(404),
            ):
                with self.assertRaisesRegex(ValueError, "--recreate-missing"):
                    asyncio.run(local_accounts.saved_identity(state, False))
                self.assertEqual(state.read_text(), original)
                self.assertIsNone(
                    asyncio.run(local_accounts.saved_identity(state, True))
                )
            self.assertFalse(state.exists())
            self.assertEqual(len(list(Path(directory).glob("*.previous-*"))), 1)

    def test_identity_service_failure_never_discards_enrollment_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "account.json"
            original = '{"identity":"old","request_id":"request"}'
            state.write_text(original)
            with patch.object(
                local_accounts,
                "_private_request",
                side_effect=local_accounts.IdentityApiError(503),
            ):
                with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                    asyncio.run(local_accounts.saved_identity(state, True))
            self.assertEqual(state.read_text(), original)

    def test_credentials_and_authorization_references_have_separate_owners(
        self,
    ) -> None:
        payload = identity_payload(
            "tester@example.invalid", "a-long-test-password"
        )
        self.assertEqual(payload["traits"], {"email": "tester@example.invalid"})
        bound = bind_identity(
            {
                "traits": payload["traits"],
                "state": "active",
                "schema_id": "local",
            },
            {"organization": "tenant", "owner": "user"},
        )
        self.assertEqual(
            bound["metadata_admin"],
            {"galadril": {"tenant_id": "tenant", "user_id": "user"}},
        )
        self.assertNotIn("credentials", bound)
        with self.assertRaises(ValueError):
            bind_identity({}, {"organization": "../other", "owner": "user"})


if __name__ == "__main__":
    unittest.main()
