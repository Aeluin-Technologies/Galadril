"""Reject bearer tokens issued for another MCP, issuer, client or scope."""

import json
import time
import unittest

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from galadril_mcp_core.auth import GatewayVerifier, MCPAuth, OAuthVerifier


class MCPAuthTest(unittest.IsolatedAsyncioTestCase):
    def test_oauth_resource_configuration_rejects_ambiguous_urls(self) -> None:
        baseline = {
            "issuer": "https://auth.example",
            "resource": "https://mcp.example/law/mcp",
            "jwks_url": "https://auth.example/jwks",
            "scopes": ("legal:read",),
        }
        for field in ("issuer", "resource", "jwks_url"):
            for invalid in (
                "http://auth.example",
                "https:///missing",
                "https://user:secret@auth.example",
                "https://auth.example?query",
                "https://auth.example#fragment",
            ):
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaises(ValueError),
                ):
                    MCPAuth.model_validate(baseline | {field: invalid})
        for scopes in (("",), ("legal read",), ("x" * 129,)):
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                MCPAuth.model_validate(baseline | {"scopes": scopes})
        self.assertEqual(
            MCPAuth.model_validate(baseline | {"scopes": ("x" * 128,)}).scopes,
            ("x" * 128,),
        )

    async def test_native_grants_reject_foreign_resources_scopes_and_gateway_failures(
        self,
    ) -> None:
        settings = MCPAuth(
            issuer="https://auth.example",
            resource="https://mcp.example/law/mcp",
            jwks_url="https://auth.example/jwks",
            scopes=("legal:read",),
        )
        identity = {
            "active": True,
            "subject": "user",
            "tenant": "tenant",
            "generation": "generation",
            "resource": settings.resource,
            "scopes": ["legal:read"],
            "expires_in": 60,
        }
        changed: dict[str, object] = {}
        status = 200
        size = 0

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.dumps(identity | changed).encode()
            return httpx.Response(status, content=body.ljust(size, b" "))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            verifier = GatewayVerifier(
                settings, http, "https://gateway.example/tools", "fixture.mcp"
            )
            for update in (
                {"resource": "https://mcp.example/mail/mcp"},
                {"scopes": ["mail:read"]},
                {"expires_in": 0},
                {"subject": ""},
            ):
                changed = update
                self.assertIsNone(await verifier.verify_token("a" * 64))
            status = 503
            changed = {}
            self.assertIsNone(await verifier.verify_token("a" * 64))
            status, size = 200, 4096
            self.assertIsNotNone(await verifier.verify_token("a" * 64))
            size = 4097
            self.assertIsNone(await verifier.verify_token("a" * 64))

    async def test_native_grants_are_reauthorized_on_every_call(self) -> None:
        active = True
        settings = MCPAuth(
            issuer="https://auth.example",
            resource="https://mcp.example/law/mcp",
            jwks_url="https://auth.example/jwks",
            scopes=("legal:read",),
        )
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "active": active,
                    "subject": "fixture-user",
                    "tenant": "fixture-tenant",
                    "generation": "fixture-generation",
                    "resource": settings.resource,
                    "scopes": ["legal:read"],
                    "expires_in": 60,
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            verifier = GatewayVerifier(
                settings, http, "https://gateway.example/tools", "fixture.mcp"
            )
            identity = await verifier.verify_token("a" * 64)
            self.assertIsNotNone(identity)
            self.assertEqual(identity.subject, "fixture-user")
            self.assertGreaterEqual(identity.expires_at, int(time.time()) + 59)
            active = False
            self.assertIsNone(await verifier.verify_token("a" * 64))
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].headers["authorization"], "Bearer " + "a" * 64
        )
        self.assertIn(b'"capability_id":"fixture.mcp"', calls[0].content)

    async def test_signature_issuer_audience_expiry_and_scope_are_required(
        self,
    ) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
            key.public_key(), as_dict=True
        ) | {"kid": "fixture", "alg": "RS256", "use": "sig"}
        hits = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(
                200, json={"keys": [jwk, jwk | {"kid": "x" * 256}]}
            )

        settings = MCPAuth(
            issuer="https://auth.example",
            resource="https://mcp.example/law/mcp",
            jwks_url="https://auth.example/jwks",
            scopes=("legal:read",),
            clients=("chatgpt-fixture",),
        )
        claims = {
            "iss": settings.issuer,
            "aud": settings.resource,
            "exp": int(time.time()) + 300,
            "sub": "fixture-user",
            "client_id": "chatgpt-fixture",
            "scope": "legal:read",
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            verifier = OAuthVerifier(settings, http)
            valid = jwt.encode(
                claims, key, algorithm="RS256", headers={"kid": "fixture"}
            )
            self.assertIsNotNone(await verifier.verify_token(valid))
            long_kid = jwt.encode(
                claims, key, algorithm="RS256", headers={"kid": "x" * 256}
            )
            self.assertIsNotNone(await verifier.verify_token(long_kid))
            empty_length = len(
                jwt.encode(
                    claims | {"padding": ""},
                    key,
                    algorithm="RS256",
                    headers={"kid": "fixture"},
                )
            )
            estimate = (16384 - empty_length) * 3 // 4
            candidates = (
                jwt.encode(
                    claims | {"padding": "x" * size},
                    key,
                    algorithm="RS256",
                    headers={"kid": "fixture"},
                )
                for size in range(estimate - 3, estimate + 4)
            )
            longest = next(
                (token for token in candidates if len(token) == 16384), None
            )
            self.assertIsNotNone(longest)
            self.assertIsNotNone(await verifier.verify_token(longest))
            for changed in (
                {"iss": "https://foreign.example"},
                {"aud": "https://mcp.example/mail/mcp"},
                {"exp": 1},
                {"scope": "mail:read"},
                {"client_id": "foreign"},
                {"sub": ""},
                {"sub": None},
                {"client_id": ""},
                {"scope": ["legal:read", 7]},
                {"scope": None},
                {"client_id": 7},
            ):
                self.assertIsNone(
                    await verifier.verify_token(
                        jwt.encode(
                            claims | changed,
                            key,
                            algorithm="RS256",
                            headers={"kid": "fixture"},
                        )
                    )
                )
            for invalid in (
                "not-a-token",
                "",
                "a" * 16385,
                valid[:-10] + ("A" if valid[-10] != "A" else "B") + valid[-9:],
                jwt.encode(
                    claims,
                    "fixture-hmac-key-which-is-not-an-issuer-key",
                    algorithm="HS256",
                    headers={"kid": "fixture"},
                ),
                jwt.encode(claims, key, algorithm="RS256"),
                jwt.encode(
                    claims, key, algorithm="RS256", headers={"kid": "x" * 257}
                ),
                jwt.encode(
                    claims, key, algorithm="RS256", headers={"kid": "unknown"}
                ),
            ):
                self.assertIsNone(await verifier.verify_token(invalid))
            self.assertEqual(hits, 1)
            failures = (
                httpx.Response(503),
                httpx.Response(200, content=b" " * 262145),
                httpx.Response(200, json={"keys": [jwk] * 65}),
                httpx.Response(200, json={"keys": [jwk | {"use": "enc"}]}),
            )
            for failure in failures:

                def unavailable(
                    request: httpx.Request, fixture: httpx.Response = failure
                ) -> httpx.Response:
                    return fixture

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(unavailable)
                ) as unavailable_http:
                    self.assertIsNone(
                        await OAuthVerifier(
                            settings, unavailable_http
                        ).verify_token(valid)
                    )


if __name__ == "__main__":
    unittest.main()
