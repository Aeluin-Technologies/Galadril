"""Authorized document delivery for explicitly deployed input services."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class IngestionError(RuntimeError):
    """Keeps credentials, presigned URLs and provider payloads out of error logs."""


class IngestionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    authentication: Literal["bearer", "service"] = "bearer"
    gateway_url: str
    storage_origins: tuple[str, ...] = Field(min_length=1, max_length=16)
    tenant_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    subject: str = Field(min_length=1, max_length=256)
    group_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    timeout_seconds: float = Field(default=60, ge=0.001, le=300)
    max_bytes: int = Field(default=32 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)

    @model_validator(mode="after")
    def deployment_urls(self) -> IngestionSettings:
        gateway = urlsplit(self.gateway_url)
        expected_path = (
            "/internal/utils/ingestion"
            if self.authentication == "service"
            else "/graphql"
        )
        if gateway.path != expected_path or gateway.query or gateway.fragment:
            raise ValueError(
                "Gateway must be an authenticated GraphQL endpoint"
            )
        for value in (self.gateway_url, *self.storage_origins):
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError("Invalid deployment URL")
        for value in self.storage_origins:
            parsed = urlsplit(value)
            if parsed.path or parsed.query:
                raise ValueError("Storage allowlist entries must be origins")
        if "/" in self.tenant_id or self.tenant_id in {".", ".."}:
            raise ValueError("Invalid tenant identity")
        return self


class _Envelope(BaseModel):
    data: dict[str, JsonValue] | None = None
    errors: list[JsonValue] = Field(default_factory=list)


class _Staging(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stagingKey: str = Field(max_length=2048)
    uploadUrl: str = Field(max_length=16384)


@dataclass(frozen=True, slots=True)
class IngestionReceipt:
    key: str


class IngestionClient:
    """Binds every upload to operator-provided tenant, account and input group.

    The token provider is called for each mutation so long-running services can
    rotate credentials. A receipt is returned only after Gateway finalization;
    source checkpoints must advance after this receipt and tolerate redelivery.
    """

    __slots__ = ("_settings", "_http", "_token")

    def __init__(
        self,
        settings: IngestionSettings,
        http: httpx.AsyncClient,
        token: Callable[[], Awaitable[str]],
    ) -> None:
        self._settings = settings
        self._http = http
        self._token = token

    async def _mutation(
        self, query: str, variables: dict[str, str]
    ) -> dict[str, JsonValue]:
        token = await self._token()
        if not token or any(char in token for char in "\r\n"):
            raise IngestionError("Ingestion credential unavailable")
        service = self._settings.authentication == "service"
        payload: dict[str, JsonValue] = {"query": query, "variables": variables}
        if service:
            if query.startswith("mutation($group:String!)"):
                operation = "stage"
            elif query.startswith("mutation($staging:String!"):
                operation = "complete"
            else:
                raise IngestionError("Unknown ingestion operation")
            payload = {
                "tenant": self._settings.tenant_id,
                "user": self._settings.subject,
                "operation": operation,
                **variables,
            }
        request = httpx.Request(
            "POST",
            self._settings.gateway_url,
            headers={"x-utils-key": token}
            if service
            else {"authorization": f"Bearer {token}"},
            json=payload,
        )
        response = await self._http.send(
            request, stream=True, auth=None, follow_redirects=False
        )
        try:
            if response.status_code != 200:
                raise IngestionError(
                    "Ingestion authorization or transport failed"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 32768:
                    raise IngestionError("Ingestion response exceeds limit")
                body.extend(chunk)
            envelope = _Envelope.model_validate_json(body)
            if envelope.errors or envelope.data is None:
                raise IngestionError("Ingestion request rejected")
            return envelope.data
        finally:
            await response.aclose()

    async def upload(
        self, content: bytes, *, name: str, content_type: str
    ) -> IngestionReceipt:
        settings = self._settings
        if (
            not content
            or len(content) > settings.max_bytes
            or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}", name)
            or name.endswith(".")
            or not re.fullmatch(
                r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", content_type
            )
        ):
            raise IngestionError("Invalid ingestion document")
        with trace.get_tracer("galadril.plugins").start_as_current_span(
            "plugin.ingestion.upload",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                async with asyncio.timeout(settings.timeout_seconds):
                    data = await self._mutation(
                        "mutation($group:String!){requestStagingUpload(groupId:$group){uploadUrl stagingKey}}",
                        {"group": settings.group_id},
                    )
                    staging = _Staging.model_validate(
                        data.get("requestStagingUpload")
                    )
                    prefix = f"{settings.tenant_id}/{quote(settings.subject, safe='')}/{settings.group_id}/"
                    nonce = staging.stagingKey.removeprefix(prefix)
                    if not staging.stagingKey.startswith(
                        prefix
                    ) or not re.fullmatch(r"[0-9a-fA-F]{32}", nonce):
                        raise IngestionError("Ingestion staging scope mismatch")
                    url = urlsplit(staging.uploadUrl)
                    if (
                        f"{url.scheme}://{url.netloc}"
                        not in settings.storage_origins
                        or url.username
                        or url.password
                        or url.fragment
                    ):
                        raise IngestionError(
                            "Ingestion storage origin rejected"
                        )
                    # Raw requests prevent shared-client auth, cookies and redirects
                    # from crossing the Gateway-to-object-storage trust boundary.
                    request = httpx.Request(
                        "PUT",
                        staging.uploadUrl,
                        headers={"content-type": content_type},
                        content=content,
                    )
                    response = await self._http.send(
                        request, auth=None, follow_redirects=False, stream=True
                    )
                    try:
                        if not 200 <= response.status_code < 300:
                            raise IngestionError(
                                "Ingestion storage upload failed"
                            )
                    finally:
                        await response.aclose()
                    data = await self._mutation(
                        "mutation($staging:String!,$name:String!,$group:String!){completeUpload(stagingKey:$staging,targetName:$name,groupId:$group)}",
                        {
                            "staging": staging.stagingKey,
                            "name": name,
                            "group": settings.group_id,
                        },
                    )
                    key = data.get("completeUpload")
                    if (
                        not isinstance(key, str)
                        or key
                        != f"{settings.tenant_id}/raw/{settings.group_id}/{name}"
                    ):
                        raise IngestionError("Invalid ingestion receipt")
                    return IngestionReceipt(key)
            except Exception:
                span.set_status(StatusCode.ERROR)
                raise IngestionError("Document ingestion failed") from None
