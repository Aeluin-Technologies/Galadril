"""Exercise delegated plan credentials without contacting OpenAI or billing APIs."""

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from galadril_scribe.chatgpt_accounts import AccountFailure, ChatGPTAccounts


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_registration_uses_pkce_and_consumes_callback_once(
    tmp_path: Path,
) -> None:
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key())
    )
    public["kid"] = "fixture"
    nonce = ""
    exchanges = 0
    verifier = ""

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal exchanges, verifier
        assert request.url.host == "auth.openai.com"
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.openai.com",
                    "authorization_endpoint": "https://auth.openai.com/api/accounts/authorize",
                    "token_endpoint": "https://auth.openai.com/api/accounts/oauth/token",
                    "jwks_uri": "https://auth.openai.com/.well-known/jwks.json",
                    "revocation_endpoint": "https://auth.openai.com/api/accounts/oauth/revoke",
                },
            )
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [public]})
        assert request.url.path.endswith("oauth/token")
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["oaiapp_fixture"]
        assert form["redirect_uri"] == ["http://127.0.0.1:1455/auth/callback"]
        verifier = form["code_verifier"][0]
        exchanges += 1
        return httpx.Response(
            200,
            json={
                "access_token": "private-access-token",
                "refresh_token": "private-refresh-token",
                "id_token": jwt.encode(
                    {
                        "iss": "https://auth.openai.com",
                        "aud": "oaiapp_fixture",
                        "sub": "openai-user",
                        "email": "person@example.test",
                        "nonce": nonce,
                        "iat": int(time.time()),
                        "exp": int(time.time()) + 3600,
                    },
                    signing,
                    algorithm="RS256",
                    headers={"kid": "fixture"},
                ),
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        broker = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        launch = await broker.begin(
            "a" * 64, "http://127.0.0.1:1455/auth/callback"
        )
        query = parse_qs(urlsplit(launch.url).query)
        nonce = query["nonce"][0]
        assert query["client_id"] == ["dynamic_agent_client"]
        assert query["agent_name_hint"] == ["Galadril"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["ext_agent_host_id"][0].startswith("urn:uuid:")
        with pytest.raises(AccountFailure):
            await broker.complete(
                "invalid-state", code="code", client_id="oaiapp_fixture"
            )
        await broker.complete(
            query["state"][0], code="code", client_id="oaiapp_fixture"
        )
        assert (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
            == query["code_challenge"][0]
        )
        with pytest.raises(AccountFailure):
            await broker.complete(
                query["state"][0], code="code", client_id="oaiapp_fixture"
            )
        assert exchanges == 1
        status = await broker.status("a" * 64)
        assert status.connected
        assert status.accounts[0].label == "person@example.test"
        assert not (await broker.status("b" * 64)).connected
        credential = await broker.access("a" * 64)
        assert credential is not None
        assert credential.token.get_secret_value() == "private-access-token"
        await broker.close()
    for path in tmp_path.iterdir():
        assert "private-access-token" not in path.read_bytes().decode(
            "utf-8", errors="ignore"
        )
        assert "private-refresh-token" not in path.read_bytes().decode(
            "utf-8", errors="ignore"
        )
    assert (tmp_path / "accounts.sqlite").stat().st_mode & 0o077 == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "redirect",
    [
        "https://example.test/auth/callback",
        "http://localhost:1455/auth/callback",
        "http://127.0.0.1:1455/other",
        "http://127.0.0.1:1455/auth/callback?next=x",
    ],
)
async def test_public_flow_rejects_non_loopback_redirects(
    tmp_path: Path, redirect: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Unexpected network")
        )
    ) as client:
        broker = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        with pytest.raises(AccountFailure):
            await broker.begin("a" * 64, redirect)
        await broker.close()


@pytest.mark.anyio
async def test_pending_signins_are_bounded_and_previous_attempt_is_invalidated(
    tmp_path: Path,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Unexpected network")
        )
    ) as client:
        broker = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        first = await broker.begin(
            "a" * 64, "http://127.0.0.1:1455/auth/callback"
        )
        second = await broker.begin(
            "a" * 64, "http://127.0.0.1:1455/auth/callback"
        )
        assert first.url != second.url
        state = parse_qs(urlsplit(first.url).query)["state"][0]
        with pytest.raises(AccountFailure):
            await broker.complete(
                state, code="code", client_id="oaiapp_fixture"
            )
        for value in range(127):
            await broker.begin(
                hashlib.sha256(str(value).encode()).hexdigest(),
                "http://127.0.0.1:1455/auth/callback",
            )
        with pytest.raises(AccountFailure):
            await broker.begin("b" * 64, "http://127.0.0.1:1455/auth/callback")
        await broker.close()


class OAuthProvider:
    """Keep fixture responses realistic while exposing only bounded failure controls."""

    def __init__(self) -> None:
        self.signing = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        self.public = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(self.signing.public_key())
        )
        self.public["kid"] = "fixture"
        self.nonce = ""
        self.subject = "user"
        self.scope = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
        self.expires_in = 3600
        self.refreshes = 0
        self.revocations = 0
        self.invalid_refresh = False
        self.invalid_nonce = False

    async def transport(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            assert request.headers["authorization"] == "Bearer access-one"
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-6.1-sol",
                            "display_name": "Sol",
                            "visibility": "list",
                        },
                        {
                            "slug": "hidden",
                            "display_name": "Hidden",
                            "visibility": "hidden",
                        },
                    ]
                },
            )
        assert request.url.host == "auth.openai.com"
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [self.public]})
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.openai.com",
                    "revocation_endpoint": "https://auth.openai.com/api/accounts/oauth/revoke",
                },
            )
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["oaiapp_fixture"]
        if request.url.path.endswith("oauth/revoke"):
            self.revocations += 1
            return httpx.Response(200)
        assert request.url.path.endswith("oauth/token")
        if form["grant_type"] == ["refresh_token"]:
            self.refreshes += 1
            assert form["refresh_token"] == ["refresh-one"]
            assert "scope" not in form
            await asyncio.sleep(0.05)
            if self.invalid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "access-two",
                    "refresh_token": "refresh-two",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "access-one",
                "refresh_token": "refresh-one",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
                "scope": self.scope,
                "id_token": jwt.encode(
                    {
                        "iss": "https://auth.openai.com",
                        "aud": "oaiapp_fixture",
                        "sub": self.subject,
                        "nonce": "wrong" if self.invalid_nonce else self.nonce,
                        "iat": int(time.time()),
                        "exp": int(time.time()) + 3600,
                    },
                    self.signing,
                    algorithm="RS256",
                    headers={"kid": "fixture"},
                ),
            },
        )

    async def connect(
        self,
        broker: ChatGPTAccounts,
        owner: str = "a" * 64,
        registration: str | None = None,
    ) -> None:
        launch = await broker.begin(
            owner, "http://127.0.0.1:1455/auth/callback", registration
        )
        query = parse_qs(urlsplit(launch.url).query)
        self.nonce = query["nonce"][0]
        await broker.complete(
            query["state"][0], code="code", client_id="oaiapp_fixture"
        )


@pytest.mark.anyio
async def test_refresh_rotation_is_serialized_across_broker_instances(
    tmp_path: Path,
) -> None:
    provider = OAuthProvider()
    provider.expires_in = 60
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.transport)
    ) as client:
        first = ChatGPTAccounts(tmp_path / "accounts.sqlite", b"k" * 32, client)
        second = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        await provider.connect(first)
        credentials = await asyncio.gather(
            first.access("a" * 64), second.access("a" * 64)
        )
        assert provider.refreshes == 1
        assert [
            item.token.get_secret_value() if item else None
            for item in credentials
        ] == ["access-two", "access-two"]
        await first.close()
        await second.close()


@pytest.mark.anyio
async def test_declined_plan_permission_and_invalid_nonce_never_enable_inference(
    tmp_path: Path,
) -> None:
    provider = OAuthProvider()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.transport)
    ) as client:
        broker = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        provider.invalid_nonce = True
        with pytest.raises(AccountFailure):
            await provider.connect(broker)
        assert not (await broker.status("a" * 64)).accounts
        provider.invalid_nonce = False
        provider.scope = "openid profile email"
        await provider.connect(broker)
        assert not (await broker.status("a" * 64)).connected
        assert await broker.access("a" * 64) is None
        await broker.close()


@pytest.mark.anyio
async def test_invalid_refresh_requires_reconnection_and_cannot_resurrect_on_disconnect(
    tmp_path: Path,
) -> None:
    provider = OAuthProvider()
    provider.expires_in = 60
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.transport)
    ) as client:
        broker = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        await provider.connect(broker)
        provider.invalid_refresh = True
        with pytest.raises(AccountFailure):
            await broker.access("a" * 64)
        assert not (await broker.status("a" * 64)).connected
        assert provider.refreshes == 1
        assert await broker.access("a" * 64) is None
        await broker.close()


@pytest.mark.anyio
async def test_disconnect_revokes_and_preserves_client_mapping_for_reauthorization(
    tmp_path: Path,
) -> None:
    provider = OAuthProvider()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.transport)
    ) as client:
        broker = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        await provider.connect(broker)
        registration = (await broker.status("a" * 64)).selected
        assert registration is not None
        with pytest.raises(AccountFailure):
            await broker.disconnect("b" * 64, registration)
        assert await broker.disconnect("a" * 64, registration)
        assert provider.revocations == 1
        assert await broker.access("a" * 64) is None
        launch = await broker.begin(
            "a" * 64, "http://127.0.0.1:1455/auth/callback", registration
        )
        query = parse_qs(urlsplit(launch.url).query)
        assert query["client_id"] == ["oaiapp_fixture"]
        assert "agent_name_hint" not in query
        provider.nonce = query["nonce"][0]
        provider.subject = "another-user"
        with pytest.raises(AccountFailure):
            await broker.complete(
                query["state"][0], code="code", client_id="oaiapp_fixture"
            )
        await broker.close()


@pytest.mark.anyio
async def test_account_catalog_and_quota_pause_remain_private_to_selected_owner(
    tmp_path: Path,
) -> None:
    provider = OAuthProvider()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.transport)
    ) as client:
        broker = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        await provider.connect(broker)
        assert await broker.models("a" * 64) == ("gpt-6.1-sol",)
        assert await broker.models("b" * 64) == ()
        credential = await broker.access("a" * 64)
        assert credential is not None
        await broker.exhausted("a" * 64, credential.registration)
        assert (await broker.status("a" * 64)).quota_exhausted
        assert await broker.access("a" * 64) is None
        await broker.resume("a" * 64)
        assert not (await broker.status("a" * 64)).quota_exhausted
        assert await broker.access("a" * 64) is not None
        await broker.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


@pytest.mark.anyio
async def test_token_renewal_preserves_a_concurrent_quota_pause(
    tmp_path: Path,
) -> None:
    provider = OAuthProvider()
    provider.expires_in = 60
    renewing = asyncio.Event()
    release = asyncio.Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        if b"grant_type=refresh_token" in request.content:
            renewing.set()
            await release.wait()
        return await provider.transport(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        broker = ChatGPTAccounts(
            tmp_path / "accounts.sqlite", b"k" * 32, client
        )
        await provider.connect(broker)
        registration = (await broker.status("a" * 64)).selected
        assert registration is not None
        task = asyncio.create_task(broker.access("a" * 64))
        await asyncio.wait_for(renewing.wait(), timeout=2)
        await broker.exhausted("a" * 64, registration)
        release.set()
        await task
        assert (await broker.status("a" * 64)).quota_exhausted
        assert await broker.access("a" * 64) is None
