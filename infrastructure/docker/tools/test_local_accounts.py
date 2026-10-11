"""Protects administrator-only references used by local account enrollment."""

import unittest

from infrastructure.docker.tools.local_accounts import (
    bind_identity,
    identity_payload,
)


class LocalAccountTest(unittest.TestCase):
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
