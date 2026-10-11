"""Exercises the exact lifecycle actor login helpers against native Ory services."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Mapping

import httpx
from assertions import require_mapping
from authentication import (
    authenticated_token,
    expired_token,
    initialize_authentication,
    login_user,
    token_claims,
)

from infrastructure.auth.tests.test_sso import SsoIntegrationTest


class AuthenticationTest(SsoIntegrationTest):
    async def private_api(
        self, url: str, body: Mapping[str, object] | None, method: str
    ) -> Mapping[str, object]:
        script = "fetch(process.argv[1],{method:process.argv[3],headers:{'content-type':'application/json'},body:process.argv[2]==='null'?undefined:process.argv[2]}).then(async r=>{if(!r.ok)process.exit(1);console.log(await r.text())})"
        result = await asyncio.to_thread(
            self.studio.exec_run,
            ["/nodejs/bin/node", "-e", script, url, json.dumps(body), method],
        )
        if result.exit_code:
            raise AssertionError("Private identity fixture API failed")
        return require_mapping(json.loads(result.output), "identity response")

    def test_actor_sessions_and_signed_expiration(self) -> None:
        asyncio.run(self.exercise())

    async def exercise(self) -> None:
        port = self.database.ports["5432/tcp"][0]["HostPort"]
        initialize_authentication(
            self.private_api,
            self.origin,
            f"postgres://postgres:test-password@127.0.0.1:{port}/postgres",
            self.context,
        )
        self.sql(
            "INSERT INTO iam_users(tenant_id,user_id) VALUES('debug_tenant','actor'),('other_tenant','actor');"
        )
        with self.assertRaisesRegex(RuntimeError, "has not authenticated"):
            authenticated_token("actor")
        token = await login_user("actor")
        self.assertEqual(
            token_claims(token).get("acr"), "urn:galadril:webauthn:uv"
        )
        foreign = await login_user("actor", "other_tenant")
        self.assertNotEqual(token, foreign)
        async with httpx.AsyncClient(verify=self.context) as client:
            for credential, tenant in (
                (token, "debug_tenant"),
                (foreign, "other_tenant"),
            ):
                response = await client.post(
                    self.origin + "/graphql",
                    headers={"Authorization": "Bearer " + credential},
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("X-Galadril-Tenant: " + tenant, response.text)
            expired = await expired_token("actor")
            response = await client.post(
                self.origin + "/graphql",
                headers={"Authorization": "Bearer " + expired},
            )
            self.assertEqual(response.status_code, 401)
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM iam_external_identities WHERE tenant_id='debug_tenant';"
            ),
            "1",
        )


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    return unittest.TestSuite(
        [AuthenticationTest("test_actor_sessions_and_signed_expiration")]
    )


if __name__ == "__main__":
    unittest.main()
