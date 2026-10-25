"""Resolve named recipients through authorized address books and native user questions."""

from __future__ import annotations

import asyncio

import httpx
from galadril_plugins.utils.contacts import ContactPage
from galadril_plugins.utils.models import validate_address
from pydantic import ValidationError
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai_harness.ask_user import (
    AskUserRequest,
    Question,
    QuestionOption,
    check_response,
)

from galadril_scribe.dependencies import Dependencies


async def contact_page(
    ctx: RunContext[Dependencies], query: str
) -> ContactPage:
    """The Gateway binds actor identity; neither account credentials nor sender addresses are tool inputs."""
    if not 2 <= len(query.strip()) <= 128 or any(
        not character.isprintable() for character in query
    ):
        raise ModelRetry(
            "Use a full contact name of 2 to 128 printable characters."
        )
    try:
        response = await ctx.deps.client.post(
            ctx.deps.tools_url,
            headers={"authorization": f"Bearer {ctx.deps.capability}"},
            json={
                "operation": "email",
                "request": {"operation": "contacts", "query": query.strip()},
            },
            timeout=25,
        )
        if response.status_code != 200 or len(response.content) > 65536:
            raise ValueError("Contact lookup unavailable")
        return ContactPage.model_validate_json(response.content)
    except (httpx.HTTPError, ValueError, ValidationError):
        raise ModelRetry(
            "The authorized address book is unavailable. Ask the user for the recipient's exact address; never guess it."
        ) from None


async def search_contacts(ctx: RunContext[Dependencies], query: str) -> str:
    """Find named recipients in authorized personal and organization address books. Never invent an email address; ambiguous names require ask_user before drafting."""
    return (await contact_page(ctx, query)).model_dump_json()


async def resolve_recipient(
    ctx: RunContext[Dependencies], value: str, *, refinements: int = 1
) -> str:
    if "@" in value:
        try:
            return validate_address(value)
        except ValueError:
            raise ModelRetry(
                "Provide a valid recipient address or a contact name."
            ) from None
    page = await contact_page(ctx, value)
    if not page.items:
        raise ModelRetry(
            "No authorized contact matched. Ask the user for a more precise full name or an explicit email address; never guess."
        )
    if len(page.items) == 1 and not page.more and not page.incomplete:
        return page.items[0].email
    answerer = ctx.deps.answerer
    if answerer is None:
        raise ModelRetry(
            "Several contacts may match. Ask the user to identify the recipient before preparing a draft."
        )
    candidates = page.items[:5]
    labels = tuple(
        f"{index + 1}. {item.name[:44]}"
        for index, item in enumerate(candidates)
    )
    options = tuple(
        QuestionOption(label=label, description=item.email[:200])
        for label, item in zip(labels, candidates, strict=True)
    )
    request = AskUserRequest(
        questions=(
            Question(
                header="Email recipient",
                question=f"Which contact should receive this email? Name searched: {value[:128]}."
                + (
                    " The directory results are incomplete; you can refine the name."
                    if page.more or page.incomplete
                    else ""
                ),
                options=(
                    *options,
                    QuestionOption(
                        label="Refine the search",
                        description="Provide a more precise full name or email address.",
                    ),
                ),
            ),
        )
    )
    try:
        async with asyncio.timeout(120):
            response = await answerer(request)
        check_response(request, response)
    except (TimeoutError, ValueError):
        raise ModelRetry(
            "Recipient selection was not completed. Do not prepare or send this email."
        ) from None
    if response.cancelled or not response.answers:
        raise ModelRetry(
            "The user cancelled recipient selection. Do not prepare or send this email."
        )
    answer = response.answers[0]
    if answer.custom_answer is not None:
        try:
            return validate_address(answer.custom_answer.strip())
        except ValueError:
            if refinements > 0:
                return await resolve_recipient(
                    ctx,
                    answer.custom_answer.strip(),
                    refinements=refinements - 1,
                )
            raise ModelRetry(
                "Search the more precise contact name supplied by the user before preparing the email."
            ) from None
    if answer.selected and answer.selected[0] in labels:
        return candidates[labels.index(answer.selected[0])].email
    raise ModelRetry(
        "Ask the user for a more precise full name or an explicit recipient address before preparing the email."
    )


async def resolve_recipients(
    ctx: RunContext[Dependencies], to: list[str], cc: list[str], bcc: list[str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Resolve every header before publishing any draft; reuse a choice across To, CC and BCC."""
    values = (*to, *cc, *bcc)
    if (
        not to
        or any(len(group) > 20 for group in (to, cc, bcc))
        or len({value for value in values if "@" not in value}) > 8
    ):
        raise ModelRetry(
            "Use at most twenty recipients per header and eight named contacts per draft."
        )
    resolved: dict[str, str] = {}
    for value in dict.fromkeys(values):
        resolved[value] = await resolve_recipient(ctx, value)
    return (
        tuple(resolved[value] for value in to),
        tuple(resolved[value] for value in cc),
        tuple(resolved[value] for value in bcc),
    )
