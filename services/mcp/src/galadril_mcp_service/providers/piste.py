"""Serve public legal research independently of Pengolo and private dossiers."""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import JsonValue, ValidationError

from galadril_mcp_service.providers.librejustice import (
    search_librejustice,
    shortlist,
)
from galadril_mcp_service.providers.piste_api import (
    Fund,
    PisteClient,
    PisteCredentials,
    PisteUnavailable,
    Provider,
)
from galadril_mcp_service.providers.piste_references import (
    Citation,
    verify_reference,
)


def register(server: FastMCP, http: httpx.AsyncClient) -> None:
    """Register public research without requiring secrets during discovery."""
    client: PisteClient | None = None
    concurrency = asyncio.Semaphore(8)
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    metadata = {
        "securitySchemes": [{"type": "oauth2", "scopes": ["legal:read"]}]
    }

    def piste() -> PisteClient:
        """Load secrets only when a verified caller actually invokes a research tool."""
        nonlocal client
        if client is None:
            try:
                client = PisteClient(http, PisteCredentials.load())
            except ValidationError:
                raise PisteUnavailable(
                    "PISTE configuration unavailable"
                ) from None
        return client

    def unavailable() -> dict[str, JsonValue]:
        """Missing credentials are an availability result, never a fabricated legal source."""
        logging.getLogger("galadril_plugins.mcp").warning(
            "Legal provider unavailable",
            extra={"event.name": "mcp.legal.unavailable"},
        )
        return {
            "status": "unavailable",
            "verified": False,
            "fallback": "Continue with authorized public web research or other tools. This call verified no source or citation.",
        }

    def require_legal_read() -> None:
        """The unified catalog does not grant access to a tool's data."""
        identity = get_access_token()
        if identity is None or "legal:read" not in identity.scopes:
            raise ValueError("Legal research scope required")

    async def official_search(
        provider: Provider,
        query: str,
        fund: Fund,
        at_date: date | None,
        page: int,
    ) -> dict[str, JsonValue] | None:
        try:
            async with asyncio.timeout(25):
                return await piste().search(
                    provider, query, fund=fund, at_date=at_date, page=page
                )
        except (PisteUnavailable, TimeoutError):
            return None

    @server.tool(annotations=annotations, meta=metadata)
    async def search(
        query: str,
        provider: Provider = "legifrance",
        fund: Fund = "CETAT",
        at_date: date | None = None,
        page: int = 1,
    ) -> dict[str, JsonValue]:
        """Search Légifrance and LibreJustice in parallel, or request Judilibre. Use dated funds for norms. Results are leads only. Send public legal terms without private names, addresses, case files or mailbox content."""
        require_legal_read()
        if not query.strip() or len(query) > 512 or not 1 <= page <= 100:
            raise ValueError("Invalid legal search")
        if (
            provider == "legifrance"
            and fund in {"CODE_DATE", "LODA_DATE"}
            and at_date is None
        ):
            raise ValueError(
                "Dated normative searches require an explicit date"
            )
        async with concurrency:
            if provider == "judilibre":
                return (
                    await official_search(provider, query, fund, at_date, page)
                    or unavailable()
                )
            official, libre = await asyncio.gather(
                official_search(provider, query, fund, at_date, page),
                search_librejustice(http, query, fund, at_date, page),
            )
        if official is None and libre is None:
            return unavailable()
        result = (
            official
            if official is not None
            else {
                "provider": "legifrance",
                "status": "partial",
                "legifrance_status": "unavailable",
            }
        )
        seen: set[str] = set()
        data = result.get("data")
        if isinstance(data, dict) and isinstance(
            hits := data.get("results"), list
        ):
            data["results"] = shortlist(hits, seen)
        overlap: list[str] = []
        result["librejustice"] = (
            {"status": "unavailable"}
            if libre is None
            else {
                "status": "ok",
                "hits": shortlist(list(libre), seen, overlap=overlap),
                "duplicates": overlap,
            }
        )
        result["citation_verified"] = False
        return result

    @server.tool(annotations=annotations, meta=metadata)
    async def fetch(
        identifier: str, provider: Provider = "legifrance"
    ) -> dict[str, JsonValue]:
        """Retrieve an exact official decision or article by ID. Returns its original metadata, text, retrieval date and source hash. Distinguish grounds, abstracts, rapporteur conclusions and doctrine."""
        require_legal_read()
        try:
            async with concurrency:
                return await piste().decision(provider, identifier)
        except PisteUnavailable:
            return unavailable()

    @server.tool(annotations=annotations, meta=metadata)
    async def verify_citation(citation: Citation) -> dict[str, JsonValue]:
        """Re-fetch the Légifrance source and check identity and passage. Other providers supply discovery only."""
        require_legal_read()
        if citation.provider != "legifrance":
            return {
                "status": "unverified",
                "verified": False,
                "reason": "Légifrance identifier required",
            }
        try:
            async with concurrency:
                return verify_reference(
                    citation,
                    await piste().decision(
                        citation.provider, citation.identifier
                    ),
                )
        except PisteUnavailable:
            return unavailable()
