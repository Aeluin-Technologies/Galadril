"""Read official legal APIs with deployment-owned credentials and bounded I/O."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import httpx
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

Provider = Literal["legifrance", "judilibre"]
Fund = Literal["CETAT", "JURI", "CODE_DATE", "LODA_DATE", "JORF"]


class PisteUnavailable(RuntimeError):
    """Expose an availability category without echoing credentials or queries."""


def read_private_credential(location: str) -> str:
    """Read a bounded secret from a regular, private file without following links."""
    path = Path(location)
    directory = Path(os.environ.get("MCP_CREDENTIALS_DIR", "/run/secrets"))
    if (
        not directory.is_absolute()
        or ".." in directory.parts
        or path.parent != directory
        or path.name in {"", ".."}
    ):
        raise PisteUnavailable(
            "Credential must be inside the configured secrets directory"
        )
    try:
        root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root,
            )
        finally:
            os.close(root)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_size > 8192
            ):
                raise PisteUnavailable(
                    "Credential file requires mode 0600 and bounded content"
                )
            data = source.read(8193)
            if len(data) > 8192:
                raise PisteUnavailable("Credential file exceeds its size limit")
            return data.decode().strip()
    except (OSError, UnicodeError, ValueError):
        raise PisteUnavailable(
            "Credential file is unavailable or invalid"
        ) from None


def _optional_credential_file(variable: str) -> str:
    """Keep a missing provider file from disabling the other provider."""
    if not (location := os.environ.get(variable)):
        return ""
    try:
        return read_private_credential(location)
    except PisteUnavailable:
        trace.get_current_span().add_event(
            "mcp.credential.unavailable", {"credential.setting": variable}
        )
        return ""


class PisteCredentials(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    judilibre_key: SecretStr | None = None
    environment: Literal["production", "sandbox"] = "production"

    @classmethod
    def load(cls) -> PisteCredentials:
        """Read operator secrets without exposing them to model arguments."""
        value = _optional_credential_file("LAW_PISTE_CREDENTIALS_FILE")
        if value.startswith("{"):
            return cls.model_validate_json(value)
        values = cls._fields(value)
        oauth = cls._fields(_optional_credential_file("LAW_PISTE_OAUTH_FILE"))
        credentials = {
            "judilibre_key": (values.get("API_KEY", "") if values else value)
            or os.environ.get("LAW_PISTE_API_KEY", "").strip()
            or None,
            "client_id": oauth.get("CLIENT_ID", oauth.get("CLIENT_KEY"))
            or os.environ.get("LAW_PISTE_CLIENT_ID"),
            "client_secret": oauth.get("SECRET_KEY")
            or os.environ.get("LAW_PISTE_CLIENT_SECRET"),
            "environment": os.environ.get(
                "LAW_PISTE_ENVIRONMENT", "production"
            ),
        }
        if not credentials["judilibre_key"] and not credentials["client_id"]:
            raise PisteUnavailable("PISTE credentials are not configured")
        return cls.model_validate(credentials)

    @staticmethod
    def _fields(value: str) -> dict[str, str]:
        """Accept secret-file assignments without executing shell syntax."""
        fields: dict[str, str] = {}
        for line in value.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r"\s*([A-Z_]+)\s*[=:]\s*(.*?)\s*", line)
            if match is not None:
                fields[match[1]] = match[2].strip("\"'")
        return fields


class _Token(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    access_token: SecretStr
    expires_in: int = Field(ge=1, le=86400)
    token_type: Literal["Bearer", "bearer"]


class PisteClient:
    __slots__ = (
        "http",
        "credentials",
        "origin",
        "oauth",
        "token",
        "expires",
        "lock",
    )

    def __init__(
        self, http: httpx.AsyncClient, credentials: PisteCredentials
    ) -> None:
        self.http, self.credentials = http, credentials
        sandbox = credentials.environment == "sandbox"
        self.origin = (
            "https://sandbox-api.piste.gouv.fr"
            if sandbox
            else "https://api.piste.gouv.fr"
        )
        self.oauth = (
            "https://sandbox-oauth.piste.gouv.fr/api/oauth/token"
            if sandbox
            else "https://oauth.piste.gouv.fr/api/oauth/token"
        )
        self.token: SecretStr | None = None
        self.expires = 0.0
        self.lock = asyncio.Lock()

    async def _json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, JsonValue] | None = None,
        params: dict[str, str | int] | None = None,
        data: dict[str, str] | None = None,
    ) -> dict[str, JsonValue]:
        """Bound I/O and trace outcomes without exporting queries, bodies or secrets."""
        with trace.get_tracer("galadril.mcp").start_as_current_span(
            "mcp.piste.request",
            attributes={
                "http.request.method": method,
                "server.address": httpx.URL(url).host,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                async with self.http.stream(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                    params=params,
                    data=data,
                    timeout=30,
                    follow_redirects=False,
                ) as response:
                    span.set_attribute(
                        "http.response.status_code", response.status_code
                    )
                    if response.status_code != 200:
                        raise PisteUnavailable(
                            f"PISTE returned HTTP {response.status_code}"
                        )
                    return await _response_object(response)
            except (httpx.HTTPError, ValueError):
                raise PisteUnavailable(
                    "PISTE transport or response unavailable"
                ) from None

    async def _headers(self, provider: Provider) -> dict[str, str]:
        """Serialize token renewal and keep credentials in transport headers only."""
        if provider == "judilibre":
            key = self.credentials.judilibre_key
            if key is None or not (value := key.get_secret_value().strip()):
                raise PisteUnavailable("Judilibre requires its own API key")
            return {"KeyId": value}
        async with self.lock:
            if self.token is None or time.monotonic() >= self.expires:
                if (
                    self.credentials.client_id is None
                    or self.credentials.client_secret is None
                ):
                    raise PisteUnavailable(
                        "Légifrance requires OAuth client_id and client_secret"
                    )
                result = _Token.model_validate(
                    await self._json(
                        "POST",
                        self.oauth,
                        data={
                            "grant_type": "client_credentials",
                            "scope": "openid",
                            "client_id": self.credentials.client_id.get_secret_value(),
                            "client_secret": self.credentials.client_secret.get_secret_value(),
                        },
                    )
                )
                self.token = result.access_token
                self.expires = time.monotonic() + max(0, result.expires_in - 60)
            return {"authorization": "Bearer " + self.token.get_secret_value()}

    def _envelope(
        self,
        provider: Provider,
        operation: str,
        body: dict[str, JsonValue],
        *,
        identifier: str = "",
    ) -> dict[str, JsonValue]:
        return {
            "provider": provider,
            "operation": operation,
            "id": identifier,
            "origin": self.origin,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "sha256": hashlib.sha256(
                json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "citation_verified": False,
            "data": body,
        }

    async def search(
        self,
        provider: Provider,
        query: str,
        *,
        fund: Fund = "CETAT",
        at_date: date | None = None,
        page: int = 1,
    ) -> dict[str, JsonValue]:
        """Search a fixed official origin, requiring a temporal version for norms."""
        if not query.strip() or len(query) > 512 or not 1 <= page <= 100:
            raise ValueError("Invalid legal search")
        payload = (
            _legifrance_search(query, fund, at_date, page)
            if provider == "legifrance"
            else None
        )
        headers = await self._headers(provider)
        if provider == "judilibre":
            body = await self._json(
                "GET",
                self.origin + "/cassation/judilibre/v1.0/search",
                headers=headers,
                params={
                    "query": query,
                    "page": page - 1,
                    "page_size": 10,
                    "resolve_references": "true",
                },
            )
        else:
            body = await self._json(
                "POST",
                self.origin + "/dila/legifrance/lf-engine-app/search",
                headers=headers,
                payload=payload,
            )
        return self._envelope(provider, "search", body)

    async def decision(
        self, provider: Provider, identifier: str
    ) -> dict[str, JsonValue]:
        """Fetch by provider ID; reject arbitrary model-supplied URLs."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identifier):
            raise ValueError("Invalid official document identifier")
        if provider == "legifrance" and not re.fullmatch(
            r"(?:CETATEXT|JURITEXT|LEGIARTI|JORFTEXT)\d{12}", identifier
        ):
            raise ValueError("Invalid Légifrance identifier")
        headers = await self._headers(provider)
        if provider == "judilibre":
            body = await self._json(
                "GET",
                self.origin + "/cassation/judilibre/v1.0/decision",
                headers=headers,
                params={"id": identifier, "resolve_references": "true"},
            )
        else:
            endpoint = (
                "getArticle"
                if identifier.startswith("LEGIARTI")
                else "jorf"
                if identifier.startswith("JORFTEXT")
                else "juri"
            )
            body = await self._json(
                "POST",
                self.origin
                + "/dila/legifrance/lf-engine-app/consult/"
                + endpoint,
                headers=headers,
                payload={
                    {"getArticle": "id", "jorf": "textCid", "juri": "textId"}[
                        endpoint
                    ]: identifier
                },
            )
        return self._envelope(provider, "consult", body, identifier=identifier)


async def _response_object(response: httpx.Response) -> dict[str, JsonValue]:
    """Bound decompressed API responses before JSON allocation."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > 2 * 1024 * 1024:
            raise PisteUnavailable("PISTE response exceeds limit")
        body.extend(chunk)
    value: JsonValue = json.loads(body)
    if not isinstance(value, dict):
        raise PisteUnavailable("PISTE returned an invalid object")
    return value


def _legifrance_search(
    query: str, fund: Fund, at_date: date | None, page: int
) -> dict[str, JsonValue]:
    """Build dated normative queries separately from provider transport routing."""
    if fund in {"CODE_DATE", "LODA_DATE"} and at_date is None:
        raise ValueError("Dated normative searches require an explicit date")
    filters: list[JsonValue] = []
    if at_date is not None and fund in {"CODE_DATE", "LODA_DATE"}:
        filters.append(
            {
                "facette": "DATE_VERSION",
                "singleDate": int(
                    datetime.combine(
                        at_date, datetime.min.time(), UTC
                    ).timestamp()
                    * 1000
                ),
            }
        )
    payload: dict[str, JsonValue] = {
        "fond": fund,
        "recherche": {
            "champs": [
                {
                    "typeChamp": "ALL",
                    "criteres": [
                        {
                            "typeRecherche": "TOUS_LES_MOTS_DANS_UN_CHAMP",
                            "valeur": query,
                            "operateur": "ET",
                        }
                    ],
                    "operateur": "ET",
                }
            ],
            "filtres": filters,
            "pageNumber": page,
            "pageSize": 10,
            "operateur": "ET",
            "sort": "PERTINENCE",
            "typePagination": "ARTICLE" if fund == "CODE_DATE" else "DEFAUT",
        },
    }
    return payload
