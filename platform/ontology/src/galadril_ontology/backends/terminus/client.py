"""Bounded asynchronous TerminusDB transport with explicit tenant capabilities.

Database names and credentials come only from trusted deployment configuration.
Native data-version headers provide compare-and-swap without a SQL revision DAG.
"""

from __future__ import annotations

import re
from typing import Self

import httpx
import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    model_validator,
)

from galadril_ontology.errors import (
    BranchAlreadyExistsError,
    ConcurrentHeadUpdateError,
    OntologyError,
    OntologyNotFoundError,
)

logger = structlog.get_logger(__name__)
_DOCUMENTS = TypeAdapter(list[dict[str, JsonValue]])
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class TerminusDatabase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    database: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    user: str = Field(min_length=1)
    password: SecretStr


class TerminusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint: str = "http://localhost:6363"
    organization: str = Field(
        default="admin", pattern=r"^[A-Za-z0-9_-]{1,128}$"
    )
    tenants: dict[str, TerminusDatabase] = Field(default_factory=dict)
    bases: TerminusDatabase | None = None

    @model_validator(mode="after")
    def validate_scopes(self) -> Self:
        url = httpx.URL(self.endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "TerminusDB requires an HTTP endpoint without embedded credentials"
            )
        databases = [entry.database for entry in self.tenants.values()]
        if self.bases is not None:
            databases.append(self.bases.database)
        if len(set(databases)) != len(databases):
            raise ValueError(
                "Each tenant and the shared base require distinct databases"
            )
        if any(_SEGMENT.fullmatch(tenant) is None for tenant in self.tenants):
            raise ValueError("Invalid configured tenant identifier")
        if any(entry.user == "admin" for entry in self.tenants.values()):
            raise ValueError(
                "Tenant connections must use database-scoped users"
            )
        return self


class TerminusClient:
    __slots__ = ("_config", "_http", "_owns_http")

    def __init__(
        self, config: TerminusConfig, *, http: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20),
        )

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _scope(self, tenant: str | None) -> TerminusDatabase:
        scope = (
            self._config.bases
            if tenant is None
            else self._config.tenants.get(tenant)
        )
        if scope is None:
            raise OntologyNotFoundError(
                "TerminusDB tenant capability is unavailable"
            )
        return scope

    def path(
        self, tenant: str | None, ref: str = "main", *, commit: bool = False
    ) -> str:
        scope = self._scope(tenant)
        if _SEGMENT.fullmatch(ref) is None:
            raise ValueError("Invalid native branch or commit identifier")
        return f"{self._config.organization}/{scope.database}/local/{'commit' if commit else 'branch'}/{ref}"

    async def request(
        self,
        tenant: str | None,
        method: str,
        operation: str,
        *,
        ref: str = "main",
        commit: bool = False,
        params: dict[str, str] | None = None,
        body: JsonValue | list[dict[str, JsonValue]] = None,
        expected: str | None = None,
    ) -> tuple[str | None, JsonValue]:
        scope = self._scope(tenant)
        path = self.path(tenant, ref, commit=commit)
        headers = (
            {"TerminusDB-Data-Version": f"branch:{expected}"}
            if expected is not None
            else {}
        )
        try:
            async with self._http.stream(
                method,
                f"{self._config.endpoint.rstrip('/')}/api/{operation}/{path}",
                auth=(scope.user, scope.password.get_secret_value()),
                params=params,
                json=body,
                headers=headers,
            ) as response:
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise OntologyError(
                            "TerminusDB response exceeds the configured safety limit"
                        )
                if not response.is_success and b"BranchExistsError" in payload:
                    raise BranchAlreadyExistsError(
                        "TerminusDB branch already exists"
                    )
                if not response.is_success and any(
                    marker in payload
                    for marker in (
                        b"UnresolvableAbsoluteDescriptor",
                        b"BranchDoesNotExistError",
                    )
                ):
                    raise OntologyNotFoundError(
                        "TerminusDB branch is unavailable"
                    )
                if response.status_code in {409, 412} or (
                    response.status_code == 400 and b"DataVersion" in payload
                ):
                    raise ConcurrentHeadUpdateError(
                        "TerminusDB branch changed; reload before retrying"
                    )
                if response.status_code == 404:
                    raise OntologyNotFoundError(
                        "TerminusDB resource is unavailable"
                    )
                if not response.is_success:
                    raise OntologyError(
                        f"TerminusDB operation failed (HTTP {response.status_code})"
                    )
                header = response.headers.get("TerminusDB-Data-Version")
                version = (
                    header.split(":", 1)[1]
                    if header and ":" in header
                    else None
                )
                if version is not None and _SEGMENT.fullmatch(version) is None:
                    raise OntologyError("Invalid TerminusDB data version")
                result = _JSON.validate_json(payload) if payload else None
                return version, result
        except httpx.HTTPError as error:
            logger.error(
                "terminus_request_failed",
                tenant_id=tenant,
                operation=operation,
                error_type=type(error).__name__,
            )
            raise OntologyError("TerminusDB is unavailable") from error

    async def read(
        self, tenant: str | None, *, ref: str = "main", commit: bool = False
    ) -> tuple[str, list[dict[str, JsonValue]]]:
        version, payload = await self.request(
            tenant,
            "GET",
            "document",
            ref=ref,
            commit=commit,
            params={"as_list": "true"},
        )
        if version is None:
            raise OntologyError("TerminusDB omitted the native data version")
        return version, _DOCUMENTS.validate_python(payload)

    async def write(
        self,
        tenant: str | None,
        document: dict[str, JsonValue] | list[dict[str, JsonValue]],
        *,
        expected: str,
        author: str,
        message: str,
        ref: str = "main",
    ) -> str:
        version, _ = await self.request(
            tenant,
            "PUT",
            "document",
            ref=ref,
            params={
                "raw_json": "true",
                "create": "true",
                "author": author,
                "message": message,
            },
            body=document,
            expected=expected,
        )
        if version is None:
            raise OntologyError("TerminusDB omitted the committed data version")
        logger.info(
            "terminus_revision_committed",
            tenant_id=tenant,
            branch=ref,
            revision_id=version,
        )
        return version


def document_named(
    documents: list[dict[str, JsonValue]], identity: str
) -> dict[str, JsonValue]:
    """Accepts compact and expanded TerminusDB document identifiers."""
    for document in documents:
        identifier = document.get("@id")
        if (
            identifier == identity
            or identifier == f"terminusdb:///data/{identity}"
        ):
            return document
    raise OntologyNotFoundError("TerminusDB document is unavailable")
