"""Serve public legal research independently of Pengolo and private dossiers."""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import JsonValue

from galadril_mcp_service.providers.piste_api import (
    Fund,
    PisteClient,
    PisteCredentials,
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
            client = PisteClient(http, PisteCredentials.load())
        return client

    @server.tool(annotations=annotations, meta=metadata)
    async def search(
        query: str,
        provider: Provider = "legifrance",
        fund: Fund = "CETAT",
        at_date: date | None = None,
        page: int = 1,
    ) -> dict[str, JsonValue]:
        """Search Légifrance decisions, codes, legislation, or Judilibre. Use dated funds with the relevant date for norms. Search results are leads; fetch the exact ID before citing. Never send private party details when a legal query suffices."""
        async with concurrency:
            return await piste().search(
                provider, query, fund=fund, at_date=at_date, page=page
            )

    @server.tool(annotations=annotations, meta=metadata)
    async def fetch(
        identifier: str, provider: Provider = "legifrance"
    ) -> dict[str, JsonValue]:
        """Retrieve an exact official decision or article by ID. Returns its original metadata, text, retrieval date and source hash. Distinguish grounds, abstracts, rapporteur conclusions and doctrine."""
        async with concurrency:
            return await piste().decision(provider, identifier)

    @server.tool(annotations=annotations, meta=metadata)
    async def verify_citation(citation: Citation) -> dict[str, JsonValue]:
        """Re-fetch the source and verify the claimed identity and passage. Applicability and temporal/material scope still require separate legal analysis."""
        async with concurrency:
            return verify_reference(
                citation,
                await piste().decision(citation.provider, citation.identifier),
            )
