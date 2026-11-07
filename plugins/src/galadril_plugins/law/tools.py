"""Expose dossier validation without accessing files or starting external actions."""

from galadril_plugins.law.dossier import Dossier
from galadril_plugins.law.writings import render_filing
from galadril_scribe.dependencies import Dependencies
from pydantic import JsonValue
from pydantic_ai.toolsets import FunctionToolset


def tools() -> FunctionToolset[Dependencies]:
    result: FunctionToolset[Dependencies] = FunctionToolset()

    @result.tool_plain
    def validate_dossier(dossier: Dossier) -> dict[str, JsonValue]:
        """Validate the structure of a complete administrative dossier. Confirm evidence and use neutral party references before calling."""
        return dossier.wire()

    @result.tool_plain
    def render_litigation_filing(dossier: Dossier, filing_id: str) -> str:
        """Render a validated petition or brief with a separate information.tex file for private details."""
        return render_filing(dossier, filing_id)

    return result
