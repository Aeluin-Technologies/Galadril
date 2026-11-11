"""Use the central MCP with attenuated user authority, never PISTE credentials."""

import os
from datetime import date
from typing import Literal

from galadril_mcp_core.client import MCPUnavailable, delegated_call
from galadril_mcp_core.legal import DecisionSearch, NormRead, NormSearch
from galadril_scribe.dependencies import Dependencies
from galadril_scribe.extensions import MediatedToolset
from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

Provider = Literal["legifrance", "judilibre"]
Fund = Literal["CETAT", "JURI", "CODE_DATE", "LODA_DATE", "JORF"]


def research_tools() -> FunctionToolset[Dependencies]:
    result = MediatedToolset()
    result.replayable_names = (
        "search_decisions",
        "get_decision",
        "search_norms",
        "get_norm",
        "search_official_law",
        "consult_official_law",
        "verify_official_citation",
    )

    async def call(
        ctx: RunContext[Dependencies],
        tool: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if ctx.deps.progress is not None:
            await ctx.deps.progress("mcp")
        try:
            return await delegated_call(
                ctx.deps.client,
                ctx.deps.tools_url,
                ctx.deps.capability,
                capability_id="law.mcp.research",
                resource="https://mcp.gravitalia.com/law/mcp",
                endpoint=os.environ.get(
                    "LAW_MCP_URL", "http://127.0.0.1:8098/law/mcp"
                ),
                tool=tool,
                arguments=arguments,
            )
        except MCPUnavailable:
            return {
                "status": "unavailable",
                "fallback": "Use authorized public web research. No source or citation has been verified by this failed call.",
            }

    @result.tool
    async def search_decisions(
        ctx: RunContext[Dependencies], request: DecisionSearch
    ) -> dict[str, JsonValue]:
        """Search public decisions with separate solution, court and date filters. Send only public legal wording and filters, excluding any private dossier facts or identities."""
        return await call(
            ctx,
            "search_decisions",
            {"request": request.model_dump(mode="json")},
        )

    @result.tool
    async def get_decision(
        ctx: RunContext[Dependencies], url: str
    ) -> dict[str, JsonValue]:
        """Read the full public decision and appeal chronology. Verify French citations separately against Légifrance."""
        return await call(ctx, "get_decision", {"url": url})

    @result.tool
    async def search_norms(
        ctx: RunContext[Dependencies], request: NormSearch
    ) -> dict[str, JsonValue]:
        """Find public norms by code, jurisdiction, as_of and valid_from. Exclude private details from the query."""
        return await call(
            ctx, "search_norms", {"request": request.model_dump(mode="json")}
        )

    @result.tool
    async def get_norm(
        ctx: RunContext[Dependencies], request: NormRead
    ) -> dict[str, JsonValue]:
        """Read a public norm using the applicable legal date and compare it with the official publisher."""
        return await call(
            ctx, "get_norm", {"request": request.model_dump(mode="json")}
        )

    @result.tool
    async def search_official_law(
        ctx: RunContext[Dependencies],
        query: str,
        provider: Provider = "legifrance",
        fund: Fund = "CETAT",
        at_date: date | None = None,
        page: int = 1,
    ) -> dict[str, JsonValue]:
        """Search Légifrance and LibreJustice together by default. Use a short public legal question or keywords in French. Normative dated funds require at_date. Keep private case details out of the query. Verify each citation against Légifrance."""
        return await call(
            ctx,
            "search",
            {
                "provider": provider,
                "query": query,
                "fund": fund,
                "at_date": at_date.isoformat() if at_date else None,
                "page": page,
            },
        )

    @result.tool
    async def consult_official_law(
        ctx: RunContext[Dependencies], provider: Provider, identifier: str
    ) -> dict[str, JsonValue]:
        """Read the exact official decision or article. Verify identity, grounds, factual scope and temporal applicability separately."""
        return await call(
            ctx, "fetch", {"provider": provider, "identifier": identifier}
        )

    @result.tool
    async def verify_official_citation(
        ctx: RunContext[Dependencies],
        provider: Provider,
        identifier: str,
        number: str,
        court: str,
        decision_date: date,
        passage: str,
    ) -> dict[str, JsonValue]:
        """Check source identity and an exact passage in Légifrance. Use provider=legifrance and its official identifier; discovery results never verify a citation."""
        return await call(
            ctx,
            "verify_citation",
            {
                "citation": {
                    "provider": provider,
                    "identifier": identifier,
                    "number": number,
                    "court": court,
                    "date": decision_date.isoformat(),
                    "passage": passage,
                }
            },
        )

    return result
