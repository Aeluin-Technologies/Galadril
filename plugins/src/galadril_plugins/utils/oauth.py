"""OAuth consent uses fixed issuers, PKCE and owner-bound single-use state."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx
from galadril_plugins.utils.models import Credentials, Principal
from galadril_plugins.utils.store import MailError, MailStore
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

PROVIDERS = {
    "google": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "https://mail.google.com/",
    ),
    "microsoft": (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send",
    ),
}


class OAuthClient(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    client_id: str = Field(min_length=1, max_length=1024)
    client_secret: SecretStr
    redirect_uri: str

    @model_validator(mode="after")
    def public_redirect(self) -> OAuthClient:
        url = urlsplit(self.redirect_uri)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path != "/settings/email"
        ):
            raise ValueError(
                "OAuth requires the fixed HTTPS email settings callback"
            )
        return self


class TokenResult(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    access_token: SecretStr = Field(min_length=1, max_length=16384)
    refresh_token: SecretStr | None = Field(default=None, max_length=16384)
    expires_in: int = Field(default=3600, ge=1, le=86400)
    token_type: str = "Bearer"


class OAuthBroker:
    __slots__ = ("store", "clients")

    def __init__(
        self, store: MailStore, clients: Mapping[str, OAuthClient]
    ) -> None:
        if any(name not in PROVIDERS for name in clients):
            raise MailError("Unsupported OAuth provider")
        self.store, self.clients = store, clients
        with store.database() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS oauth_states(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,user TEXT NOT NULL,payload TEXT NOT NULL,expires INTEGER NOT NULL)"
            )

    def start(self, actor: Principal, provider: str, address: str = "") -> str:
        client = self.clients.get(provider)
        if client is None:
            raise MailError("OAuth provider is not configured")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        identifier = hashlib.sha256(state.encode()).hexdigest()
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        with self.store.database() as db:
            db.execute(
                "DELETE FROM oauth_states WHERE expires<?", (int(time.time()),)
            )
            count = db.execute(
                "SELECT count(*) FROM oauth_states WHERE tenant=? AND user=?",
                (actor.tenant, actor.user),
            ).fetchone()[0]
            if count >= 8:
                raise MailError("Too many pending consent requests")
            data = json.dumps(
                {"provider": provider, "verifier": verifier, "address": address}
            ).encode()
            db.execute(
                "INSERT INTO oauth_states VALUES(?,?,?,?,?)",
                (
                    identifier,
                    actor.tenant,
                    actor.user,
                    self.store.encrypt(
                        actor.tenant, actor.user, "oauth:" + identifier, data
                    ),
                    int(time.time()) + 600,
                ),
            )
        return (
            PROVIDERS[provider][0]
            + "?"
            + urlencode(
                {
                    "client_id": client.client_id,
                    "redirect_uri": client.redirect_uri,
                    "response_type": "code",
                    "scope": PROVIDERS[provider][2],
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "access_type": "offline",
                    "prompt": "consent",
                    "login_hint": address,
                }
            )
        )

    def consume(self, actor: Principal, state: str) -> dict[str, str]:
        identifier = hashlib.sha256(state.encode()).hexdigest()
        with self.store.database() as db:
            row = db.execute(
                "SELECT payload FROM oauth_states WHERE id=? AND tenant=? AND user=? AND expires>=?",
                (identifier, actor.tenant, actor.user, int(time.time())),
            ).fetchone()
            if row is None:
                raise MailError(
                    "Consent expired or belongs to another identity"
                )
            value: dict[str, str] = json.loads(
                self.store.decrypt(
                    actor.tenant,
                    actor.user,
                    "oauth:" + identifier,
                    row["payload"],
                )
            )
            db.execute("DELETE FROM oauth_states WHERE id=?", (identifier,))
            return value

    async def _token(
        self, http: httpx.AsyncClient, provider: str, data: dict[str, str]
    ) -> TokenResult:
        client = self.clients.get(provider)
        if client is None:
            raise MailError("OAuth provider is not configured")
        data.update(
            client_id=client.client_id,
            client_secret=client.client_secret.get_secret_value(),
        )
        request = httpx.Request("POST", PROVIDERS[provider][1], data=data)
        response = await http.send(
            request, auth=None, follow_redirects=False, stream=True
        )
        try:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 32768:
                    raise MailError("OAuth response exceeds limit")
                body.extend(chunk)
            if response.status_code != 200:
                raise MailError("Email consent needs renewal")
            result = TokenResult.model_validate_json(body)
            if result.token_type.lower() != "bearer":
                raise MailError("OAuth token type rejected")
            return result
        finally:
            await response.aclose()

    async def finish(
        self, http: httpx.AsyncClient, actor: Principal, state: str, code: str
    ) -> tuple[str, Credentials]:
        value = self.consume(actor, state)
        provider = value["provider"]
        result = await self._token(
            http,
            provider,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": value["verifier"],
                "redirect_uri": self.clients[provider].redirect_uri,
            },
        )
        if result.refresh_token is None:
            raise MailError(
                "Grant offline access to enable background email synchronization"
            )
        auth: Literal["google", "microsoft"] = (
            "google" if provider == "google" else "microsoft"
        )
        return provider, Credentials(
            username=value["address"],
            password=result.access_token,
            auth=auth,
            refresh_token=result.refresh_token,
            expires_at=int(time.time()) + result.expires_in,
        )

    async def refresh(
        self, http: httpx.AsyncClient, credentials: Credentials
    ) -> Credentials:
        if (
            credentials.auth == "password"
            or credentials.expires_at > int(time.time()) + 120
        ):
            return credentials
        if credentials.refresh_token is None:
            raise MailError("Email consent needs renewal")
        result = await self._token(
            http,
            credentials.auth,
            {
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token.get_secret_value(),
                "scope": PROVIDERS[credentials.auth][2],
            },
        )
        return credentials.model_copy(
            update={
                "password": result.access_token,
                "refresh_token": result.refresh_token
                or credentials.refresh_token,
                "expires_at": int(time.time()) + result.expires_in,
            }
        )
