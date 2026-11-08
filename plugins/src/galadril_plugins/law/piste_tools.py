"""Use the central MCP with attenuated user authority, never PISTE credentials."""

import os
from datetime import date
from typing import Literal

from galadril_mcp_core.client import MCPUnavailable, delegated_call
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
    async def search_official_law(
        ctx: RunContext[Dependencies],
        provider: Provider,
        query: str,
        fund: Fund = "CETAT",
        at_date: date | None = None,
        page: int = 1,
    ) -> dict[str, JsonValue]:
        """Search Légifrance or Judilibre through the central MCP. Normative dated funds require at_date. Results are leads; consult exact returned IDs before citing. Keep using authorized web research for ArianeWeb, Open Data, exhibits and doctrine."""
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
        """Check source identity and an exact passage through MCP. This never establishes legal applicability."""
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
