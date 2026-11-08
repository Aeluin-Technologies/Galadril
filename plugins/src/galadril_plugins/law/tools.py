"""Expose legal workflows while Scribe retains execution and source authority."""

from importlib.resources import files

from galadril_plugins.law.dossier import Dossier
from galadril_plugins.law.writings import render_filing
from galadril_scribe.dependencies import Dependencies
from galadril_scribe.workflows import WorkflowToolset
from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset


def tools() -> FunctionToolset[Dependencies]:
    """Load plugin-owned methods and legal vocabulary before any model runs."""
    from galadril_plugins.law.instruction import instruct
    from galadril_plugins.law.piste_tools import research_tools

    reasoning = (
        files("galadril_plugins.law")
        .joinpath("skills/administratif/raisonnement.md")
        .read_text()
    )
    incidence = (
        files("galadril_plugins.law")
        .joinpath("skills/administratif/incidence-vices.md")
        .read_text()
    )
    result = WorkflowToolset(
        instructions=reasoning,
        skill_ids=(
            "law.skill.faits",
            "law.skill.recevabilite",
            "law.skill.jurisprudence",
            "law.skill.veille",
            "law.skill.partie-adverse",
            "law.skill.redaction",
            "law.skill.citations",
            "law.skill.relecture",
        ),
        research_factory=research_tools,
        role_instructions=dict.fromkeys(
            ("objections", "procedure", "rejet"), incidence
        ),
        replayable_names=(
            "instruct_litigation",
            "validate_dossier",
            "render_litigation_filing",
        ),
    )

    @result.tool
    async def instruct_litigation(
        ctx: RunContext[Dependencies],
        memorandum: str,
        evidence: tuple[int, ...] = (),
        reviewer_evidence: tuple[int, ...] = (),
        revision: str = "initial",
        refinements: int = 1,
    ) -> dict[str, object]:
        """Instruct an original memorandum by ground, draft, commission three independent defenses, revise and verify. Evidence indices refer only to current attachments. Reviewer exhibits need explicit sharing. Reuse the same revision when resuming; change it only for a deliberately changed scope. Return a human-review draft, then use recoursadmin to compile on request."""
        if ctx.deps.tasks is None:
            raise ValueError(
                "Scoped delegation requires an active specialist workflow"
            )
        return await instruct(
            ctx.deps.tasks,
            memorandum,
            evidence,
            reviewer_evidence,
            revision=revision,
            refinements=refinements,
        )

    @result.tool_plain
    def validate_dossier(dossier: Dossier) -> dict[str, JsonValue]:
        """Validate the structure of a complete administrative dossier. Confirm evidence and use neutral party references before calling."""
        return dossier.wire()

    @result.tool_plain
    def render_litigation_filing(dossier: Dossier, filing_id: str) -> str:
        """Render a validated petition or brief with a separate information.tex file for private details."""
        return render_filing(dossier, filing_id)

    return result
