"""Personal-installation OAuth credentials stay outside conversational authority."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_DIRECT_SCOPE = "chatgpt.tokens.use.direct"


class AccountFailure(RuntimeError):
    """Provider details and credentials must not cross the public error boundary."""


class _InvalidGrant(AccountFailure):
    """Only a definitive invalid grant may discard a renewable credential."""


class AccountSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    label: str
    connected: bool


class AccountStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = True
    connected: bool = False
    quota_exhausted: bool = False
    selected: str | None = None
    accounts: tuple[AccountSummary, ...] = ()
    usage_url: str = "https://chatgpt.com/settings/usage"


class AuthorizationLaunch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    url: str
    expires_at: int


class AccessCredential(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    token: SecretStr
    registration: str


class _Tokens(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    access_token: SecretStr = Field(min_length=1, max_length=32768)
    refresh_token: SecretStr | None = Field(default=None, max_length=32768)
    id_token: SecretStr | None = Field(default=None, max_length=32768)
    token_type: Literal["Bearer"]
    expires_in: int = Field(ge=60, le=7200)
    earliest_refresh_at: int = Field(default=0, ge=0)
    scope: str = Field(default="", max_length=2048)


class _Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str
    client_id: str
    subject: str
    label: str
    tokens: _Tokens | None = None
    expires_at: int = 0
    quota_exhausted: bool = False
    models: tuple[str, ...] = Field(default=(), max_length=256)
    models_until: int = 0


class _Pending(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    verifier: SecretStr
    nonce: str
    redirect: str
    registration: _Registration | None = None


class _Identity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sub: str = Field(min_length=1, max_length=256)
    nonce: str = Field(min_length=1, max_length=256)
    email: str | None = Field(default=None, max_length=256)


def _secret_json(value: object) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    raise TypeError("Unsupported credential value")


def _owner(value: str) -> str:
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise AccountFailure("Invalid account scope")
    return value


def validate_callback(value: str) -> str:
    """The public OSS contract accepts an exact loopback callback, not web hosting."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and 1024 <= parsed.port <= 65535
            and parsed.path == "/auth/callback"
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise AccountFailure("Public OAuth requires a loopback callback")
    return value


class ChatGPTAccounts:
    """SQLite leases serialize rotating tokens across workers of one installation."""

    __slots__ = (
        "path",
        "cipher",
        "client",
        "ready",
        "initializing",
        "jwks",
        "jwks_until",
        "outcomes",
    )

    def __init__(
        self, path: Path, key: bytes, client: httpx.AsyncClient
    ) -> None:
        if len(key) != 32 or not path.is_absolute():
            raise AccountFailure("Invalid credential storage configuration")
        self.path = path
        self.cipher = AESGCM(key)
        self.client = client
        self.ready = False
        self.initializing = asyncio.Lock()
        self.jwks: dict[str, object] = {}
        self.jwks_until = 0.0
        self.outcomes = metrics.get_meter("galadril.scribe").create_counter(
            "scribe.chatgpt.accounts"
        )

    def _initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            self.path.parent.is_symlink()
            or self.path.parent.stat().st_mode & 0o077
        ):
            raise AccountFailure("Credential directory must be private")
        if not self.path.exists():
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.close(descriptor)
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise AccountFailure("Credential file must be private")
        with (
            closing(sqlite3.connect(self.path, timeout=2)) as database,
            database,
        ):
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            database.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('host', ?)",
                ("urn:uuid:" + str(uuid.uuid4()),),
            )
            database.execute(
                "CREATE TABLE IF NOT EXISTS pending (state TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL, expires INTEGER NOT NULL, payload BLOB NOT NULL)"
            )
            database.execute(
                "CREATE TABLE IF NOT EXISTS accounts (owner TEXT NOT NULL, id TEXT NOT NULL, selected INTEGER NOT NULL, payload BLOB NOT NULL, lease TEXT, lease_expires INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,id))"
            )

    async def _database[T](
        self, operation: Callable[[sqlite3.Connection], T]
    ) -> T:
        async with self.initializing:
            if not self.ready:
                await asyncio.to_thread(self._initialize)
                self.ready = True

        def execute() -> T:
            with (
                closing(sqlite3.connect(self.path, timeout=2)) as database,
                database,
            ):
                database.execute("BEGIN IMMEDIATE")
                return operation(database)

        try:
            return await asyncio.to_thread(execute)
        except sqlite3.Error as error:
            raise AccountFailure("Credential storage unavailable") from error

    def _encrypt(self, owner: str, value: BaseModel) -> bytes:
        plaintext = json.dumps(
            value.model_dump(), default=_secret_json, separators=(",", ":")
        ).encode()
        if len(plaintext) > 131072:
            raise AccountFailure("Credential record exceeds limit")
        nonce = secrets.token_bytes(12)
        return nonce + self.cipher.encrypt(nonce, plaintext, owner.encode())

    def _decrypt(self, owner: str, payload: bytes) -> bytes:
        try:
            return self.cipher.decrypt(
                payload[:12], payload[12:], owner.encode()
            )
        except (InvalidTag, ValueError) as error:
            raise AccountFailure("Credential record unavailable") from error

    async def _registrations(self, owner: str) -> list[_Registration]:
        rows = await self._database(
            lambda database: database.execute(
                "SELECT selected,payload FROM accounts WHERE owner=? ORDER BY selected DESC,id",
                (_owner(owner),),
            ).fetchall()
        )
        return [
            _Registration.model_validate_json(self._decrypt(owner, payload))
            for _, payload in rows
        ]

    async def status(self, owner: str) -> AccountStatus:
        registrations = await self._registrations(owner)
        selected = await self._database(
            lambda database: database.execute(
                "SELECT id FROM accounts WHERE owner=? AND selected=1", (owner,)
            ).fetchone()
        )
        current = next(
            (
                item
                for item in registrations
                if selected and item.id == selected[0]
            ),
            None,
        )
        return AccountStatus(
            quota_exhausted=current.quota_exhausted if current else False,
            connected=current is not None
            and current.tokens is not None
            and _DIRECT_SCOPE in current.tokens.scope.split(),
            selected=current.id if current else None,
            accounts=tuple(
                AccountSummary(
                    id=item.id,
                    label=item.label,
                    connected=item.tokens is not None
                    and _DIRECT_SCOPE in item.tokens.scope.split(),
                )
                for item in registrations
            ),
        )

    async def begin(
        self, owner: str, redirect: str, registration: str | None = None
    ) -> AuthorizationLaunch:
        owner = _owner(owner)
        redirect = validate_callback(redirect)
        saved = None
        if registration is not None:
            saved = next(
                (
                    item
                    for item in await self._registrations(owner)
                    if item.id == registration
                ),
                None,
            )
            if saved is None:
                raise AccountFailure("Account registration unavailable")
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(32)
        pending = _Pending(
            verifier=SecretStr(verifier),
            nonce=secrets.token_urlsafe(32),
            redirect=redirect,
            registration=saved,
        )
        expiry = int(time.time()) + 300
        encrypted = self._encrypt(owner, pending)

        def reserve(database: sqlite3.Connection) -> str:
            database.execute(
                "DELETE FROM pending WHERE expires<=? OR owner=?",
                (int(time.time()), owner),
            )
            if (
                database.execute("SELECT count(*) FROM pending").fetchone()[0]
                >= 128
            ):
                raise AccountFailure("Too many pending sign-ins")
            if (
                saved is None
                and database.execute(
                    "SELECT count(*) FROM accounts WHERE owner=?", (owner,)
                ).fetchone()[0]
                >= 4
            ):
                raise AccountFailure("Account registration limit reached")
            database.execute(
                "INSERT INTO pending VALUES (?,?,?,?)",
                (
                    hashlib.sha256(state.encode()).hexdigest(),
                    owner,
                    expiry,
                    encrypted,
                ),
            )
            return str(
                database.execute(
                    "SELECT value FROM metadata WHERE name='host'"
                ).fetchone()[0]
            )

        host = await self._database(reserve)
        parameters = {
            "client_id": saved.client_id if saved else "dynamic_agent_client",
            "ext_agent_host_id": host,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": pending.nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            )
            .rstrip(b"=")
            .decode(),
        }
        if saved is None:
            parameters["agent_name_hint"] = "Galadril"
        elif saved.tokens is not None and saved.tokens.id_token is not None:
            parameters["id_token_hint"] = (
                saved.tokens.id_token.get_secret_value()
            )
        self.outcomes.add(1, {"operation": "begin", "outcome": "started"})
        return AuthorizationLaunch(
            url=ISSUER + "/api/accounts/authorize?" + urlencode(parameters),
            expires_at=expiry,
        )

    async def _json(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        try:
            async with self.client.stream(
                method,
                url,
                data=data,
                headers={"authorization": "Bearer " + token} if token else {},
                follow_redirects=False,
                timeout=10,
            ) as response:
                payload = bytearray()
                async for part in response.aiter_bytes():
                    if len(payload) + len(part) > 65536:
                        raise AccountFailure("Provider response exceeds limit")
                    payload.extend(part)
                decoded: object = json.loads(payload) if payload else {}
                if not isinstance(decoded, dict):
                    raise AccountFailure("Invalid provider response")
                if not response.is_success:
                    if (
                        response.status_code == 400
                        and decoded.get("error") == "invalid_grant"
                    ):
                        raise _InvalidGrant("Account requires reconnection")
                    raise AccountFailure("Provider request refused")
                return decoded
        except (httpx.HTTPError, ValueError) as error:
            raise AccountFailure("Provider unavailable") from error

    async def complete(
        self,
        state: str,
        *,
        code: str | None = None,
        client_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if not 32 <= len(state) <= 256:
            raise AccountFailure("Invalid authorization callback")

        def consume(database: sqlite3.Connection) -> tuple[str, bytes] | None:
            row = database.execute(
                "SELECT owner,payload FROM pending WHERE state=? AND expires>?",
                (hashlib.sha256(state.encode()).hexdigest(), int(time.time())),
            ).fetchone()
            database.execute(
                "DELETE FROM pending WHERE state=?",
                (hashlib.sha256(state.encode()).hexdigest(),),
            )
            return row

        consumed = await self._database(consume)
        if consumed is None:
            raise AccountFailure("Authorization attempt expired or consumed")
        owner, payload = consumed
        pending = _Pending.model_validate_json(self._decrypt(owner, payload))
        if error is not None or not code or len(code) > 8192:
            raise AccountFailure("Authorization was not granted")
        saved = pending.registration
        issued = client_id or (saved.client_id if saved else "")
        if not re.fullmatch(r"oaiapp_[A-Za-z0-9_-]{1,240}", issued) or (
            saved and issued != saved.client_id
        ):
            raise AccountFailure("Invalid client registration")
        token_response = await self._json(
            "POST",
            ISSUER + "/api/accounts/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": issued,
                "code": code,
                "code_verifier": pending.verifier.get_secret_value(),
                "redirect_uri": pending.redirect,
                "resource": RESOURCE,
            },
        )
        try:
            tokens = _Tokens.model_validate(token_response)
            if tokens.id_token is None:
                raise AccountFailure("Missing account identity")
            if not self.jwks or time.monotonic() >= self.jwks_until:
                self.jwks = await self._json(
                    "GET", ISSUER + "/.well-known/jwks.json"
                )
                self.jwks_until = time.monotonic() + 300
            header = jwt.get_unverified_header(
                tokens.id_token.get_secret_value()
            )
            raw_keys = self.jwks.get("keys")
            if not isinstance(raw_keys, list) or len(raw_keys) > 16:
                raise AccountFailure("Invalid provider signing keys")
            keys = [
                item
                for item in raw_keys
                if isinstance(item, dict)
                and item.get("kid") == header.get("kid")
                and item.get("kty") == "RSA"
            ]
            if len(keys) != 1:
                raise AccountFailure("Unknown provider signing key")
            key = jwt.PyJWK.from_dict(keys[0], algorithm="RS256")
            claims = _Identity.model_validate(
                jwt.decode(
                    tokens.id_token.get_secret_value(),
                    key.key,
                    algorithms=["RS256"],
                    issuer=ISSUER,
                    audience=issued,
                    options={
                        "require": ["iss", "aud", "sub", "exp", "iat", "nonce"]
                    },
                )
            )
            if not hmac.compare_digest(claims.nonce, pending.nonce) or (
                saved and claims.sub != saved.subject
            ):
                raise AccountFailure("Account identity changed")
        except (
            jwt.PyJWTError,
            ValidationError,
            ValueError,
        ) as validation_error:
            raise AccountFailure(
                "Invalid account identity"
            ) from validation_error
        registration = _Registration(
            id=saved.id if saved else uuid.uuid4().hex,
            client_id=issued,
            subject=claims.sub,
            label=claims.email or "ChatGPT account",
            tokens=tokens,
            expires_at=int(time.time()) + tokens.expires_in,
        )
        encrypted = self._encrypt(owner, registration)

        def publish(database: sqlite3.Connection) -> None:
            if (
                saved is None
                and database.execute(
                    "SELECT count(*) FROM accounts WHERE owner=?", (owner,)
                ).fetchone()[0]
                >= 4
            ):
                raise AccountFailure("Account registration limit reached")
            database.execute(
                "UPDATE accounts SET selected=0 WHERE owner=?", (owner,)
            )
            database.execute(
                "INSERT INTO accounts(owner,id,selected,payload) VALUES (?,?,1,?) ON CONFLICT(owner,id) DO UPDATE SET selected=1,payload=excluded.payload,lease=NULL,lease_expires=0",
                (owner, registration.id, encrypted),
            )

        await self._database(publish)
        self.outcomes.add(
            1,
            {
                "operation": "callback",
                "outcome": "connected"
                if _DIRECT_SCOPE in tokens.scope.split()
                else "permission_declined",
            },
        )

    async def access(self, owner: str) -> AccessCredential | None:
        owner = _owner(owner)
        deadline = time.monotonic() + 12
        lease = uuid.uuid4().hex
        while time.monotonic() < deadline:

            def claim(
                database: sqlite3.Connection,
            ) -> tuple[_Registration | None, bool]:
                row = database.execute(
                    "SELECT id,payload,lease_expires FROM accounts WHERE owner=? AND selected=1",
                    (owner,),
                ).fetchone()
                if row is None:
                    return None, False
                saved = _Registration.model_validate_json(
                    self._decrypt(owner, row[1])
                )
                if (
                    saved.tokens is None
                    or saved.quota_exhausted
                    or _DIRECT_SCOPE not in saved.tokens.scope.split()
                ):
                    return None, False
                now = int(time.time())
                if saved.expires_at > now + 60 or (
                    now < saved.tokens.earliest_refresh_at
                    and saved.expires_at > now
                ):
                    return saved, False
                if now < saved.tokens.earliest_refresh_at:
                    raise AccountFailure(
                        "Credential renewal is not available yet"
                    )
                if row[2] > now:
                    return None, True
                database.execute(
                    "UPDATE accounts SET lease=?,lease_expires=? WHERE owner=? AND id=?",
                    (lease, now + 30, owner, saved.id),
                )
                return saved, True

            saved, renewal = await self._database(claim)
            if saved is None:
                if renewal:
                    await asyncio.sleep(0.05)
                    continue
                return None
            if not renewal:
                return (
                    AccessCredential(
                        token=saved.tokens.access_token, registration=saved.id
                    )
                    if saved.tokens
                    else None
                )
            if saved.tokens is None or saved.tokens.refresh_token is None:
                await self._end_lease(owner, saved, lease, clear=True)
                raise AccountFailure("Account requires reconnection")
            try:
                response = await self._json(
                    "POST",
                    ISSUER + "/api/accounts/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": saved.client_id,
                        "refresh_token": saved.tokens.refresh_token.get_secret_value(),
                        "resource": RESOURCE,
                    },
                )
                tokens = _Tokens.model_validate(
                    {
                        "scope": saved.tokens.scope,
                        "id_token": saved.tokens.id_token,
                        **response,
                    }
                )
                if (
                    tokens.refresh_token is None
                    or _DIRECT_SCOPE not in tokens.scope.split()
                ):
                    raise _InvalidGrant("Account requires reconnection")
                renewed = saved.model_copy(
                    update={
                        "tokens": tokens,
                        "expires_at": int(time.time()) + tokens.expires_in,
                    }
                )
                await self._end_lease(owner, renewed, lease)
                self.outcomes.add(
                    1, {"operation": "refresh", "outcome": "completed"}
                )
                return AccessCredential(
                    token=tokens.access_token, registration=saved.id
                )
            except _InvalidGrant:
                await self._end_lease(owner, saved, lease, clear=True)
                raise
            except (AccountFailure, ValidationError):
                await self._end_lease(owner, saved, lease)
                raise AccountFailure("Account renewal unavailable") from None
        raise AccountFailure("Account renewal is busy")

    async def _end_lease(
        self,
        owner: str,
        registration: _Registration,
        lease: str,
        *,
        clear: bool = False,
    ) -> None:
        def finish(database: sqlite3.Connection) -> None:
            row = database.execute(
                "SELECT payload FROM accounts WHERE owner=? AND id=? AND lease=?",
                (owner, registration.id, lease),
            ).fetchone()
            if row is None:
                raise AccountFailure("Account changed during renewal")
            current = _Registration.model_validate_json(
                self._decrypt(owner, row[0])
            )
            # Renewal must preserve quota pauses and catalog updates made in flight.
            merged = current.model_copy(
                update={
                    "tokens": None if clear else registration.tokens,
                    "expires_at": 0 if clear else registration.expires_at,
                }
            )
            database.execute(
                "UPDATE accounts SET payload=?,lease=NULL,lease_expires=0 WHERE owner=? AND id=? AND lease=?",
                (self._encrypt(owner, merged), owner, registration.id, lease),
            )

        await self._database(finish)

    async def select(self, owner: str, registration: str) -> AccountStatus:
        def choose(database: sqlite3.Connection) -> None:
            if (
                database.execute(
                    "SELECT 1 FROM accounts WHERE owner=? AND id=?",
                    (_owner(owner), registration),
                ).fetchone()
                is None
            ):
                raise AccountFailure("Account registration unavailable")
            database.execute(
                "UPDATE accounts SET selected=(id=?) WHERE owner=?",
                (registration, owner),
            )

        await self._database(choose)
        return await self.status(owner)

    async def disconnect(self, owner: str, registration: str) -> bool:
        def remove(database: sqlite3.Connection) -> _Registration:
            row = database.execute(
                "SELECT payload FROM accounts WHERE owner=? AND id=?",
                (_owner(owner), registration),
            ).fetchone()
            if row is None:
                raise AccountFailure("Account registration unavailable")
            saved = _Registration.model_validate_json(
                self._decrypt(owner, row[0])
            )
            cleared = saved.model_copy(update={"tokens": None, "expires_at": 0})
            database.execute(
                "UPDATE accounts SET payload=?,lease=NULL,lease_expires=0 WHERE owner=? AND id=?",
                (self._encrypt(owner, cleared), owner, registration),
            )
            database.execute("DELETE FROM pending WHERE owner=?", (owner,))
            return saved

        saved = await self._database(remove)
        if saved.tokens is None or saved.tokens.refresh_token is None:
            return True
        try:
            metadata = await self._json(
                "GET", ISSUER + "/.well-known/openid-configuration"
            )
            endpoint = metadata.get("revocation_endpoint")
            if metadata.get("issuer") != ISSUER or not isinstance(
                endpoint, str
            ):
                raise AccountFailure("Provider revocation unavailable")
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "auth.openai.com"
                or parsed.query
                or parsed.fragment
            ):
                raise AccountFailure("Invalid revocation endpoint")
            await self._json(
                "POST",
                endpoint,
                data={
                    "client_id": saved.client_id,
                    "token": saved.tokens.refresh_token.get_secret_value(),
                    "token_type_hint": "refresh_token",
                },
            )
            self.outcomes.add(
                1, {"operation": "disconnect", "outcome": "revoked"}
            )
            return True
        except AccountFailure:
            self.outcomes.add(
                1,
                {
                    "operation": "disconnect",
                    "outcome": "revocation_unconfirmed",
                },
            )
            return False

    async def _update(
        self,
        owner: str,
        registration: str,
        change: Callable[[_Registration], _Registration],
    ) -> None:
        def update(database: sqlite3.Connection) -> None:
            row = database.execute(
                "SELECT payload FROM accounts WHERE owner=? AND id=?",
                (_owner(owner), registration),
            ).fetchone()
            if row is None:
                raise AccountFailure("Account registration unavailable")
            saved = _Registration.model_validate_json(
                self._decrypt(owner, row[0])
            )
            database.execute(
                "UPDATE accounts SET payload=? WHERE owner=? AND id=?",
                (self._encrypt(owner, change(saved)), owner, registration),
            )

        await self._database(update)

    async def exhausted(self, owner: str, registration: str) -> None:
        await self._update(
            owner,
            registration,
            lambda saved: saved.model_copy(update={"quota_exhausted": True}),
        )
        self.outcomes.add(1, {"operation": "quota", "outcome": "paused"})

    async def resume(self, owner: str) -> AccountStatus:
        status = await self.status(owner)
        if status.selected is None:
            raise AccountFailure("Account registration unavailable")
        await self._update(
            owner,
            status.selected,
            lambda saved: saved.model_copy(update={"quota_exhausted": False}),
        )
        return await self.status(owner)

    async def models(self, owner: str) -> tuple[str, ...]:
        credential = await self.access(owner)
        if credential is None:
            return ()
        saved = next(
            (
                item
                for item in await self._registrations(owner)
                if item.id == credential.registration
            ),
            None,
        )
        if saved is None:
            raise AccountFailure("Account registration unavailable")
        if saved.models_until > int(time.time()):
            return saved.models
        response = await self._json(
            "GET",
            RESOURCE + "/models",
            token=credential.token.get_secret_value(),
        )
        models = response.get("models")
        if not isinstance(models, list) or len(models) > 256:
            raise AccountFailure("Invalid account model catalog")
        available: list[str] = []
        for item in models:
            if isinstance(item, dict) and item.get("visibility") == "list":
                slug = item.get("slug")
                if isinstance(slug, str) and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", slug
                ):
                    available.append(slug)
        selected = tuple(dict.fromkeys(available))
        await self._update(
            owner,
            saved.id,
            lambda current: current.model_copy(
                update={
                    "models": selected,
                    "models_until": int(time.time()) + 60,
                }
            ),
        )
        return selected

    async def close(self) -> None:
        """Short transactions own their connections; no background credential task survives."""
