"""Native Scribe toolset delegates identity through the existing Gateway lease."""

from __future__ import annotations

from typing import Literal

from galadril_scribe.dependencies import Dependencies
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset


def tools() -> FunctionToolset[Dependencies]:
    result: FunctionToolset[Dependencies] = FunctionToolset()

    @result.tool
    async def email(
        ctx: RunContext[Dependencies],
        action: Literal[
            "accounts", "messages", "message", "drafts", "save_draft", "label"
        ],
        account: str | None = None,
        message: str | None = None,
        query: str = "",
        body: str = "",
        subject: str = "",
        recipient: str = "",
        label: str = "",
    ) -> str:
        """Prepare an editable draft or read authorized mail; sending requires user review."""
        if action == "save_draft":
            from galadril_scribe.email import propose_email

            return await propose_email(
                ctx, to=[recipient], subject=subject, body=body
            )
        request: dict[str, object] = {"operation": action}
        if account:
            request["account"] = account
        if action == "messages":
            request["query"] = query
        elif action == "message":
            request["message"] = message
        elif action == "label":
            request.update(message=message, label=label)
        response = await ctx.deps.client.post(
            ctx.deps.tools_url,
            headers={"authorization": f"Bearer {ctx.deps.capability}"},
            json={"operation": "email", "request": request},
        )
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise ValueError("Authorized email operation unavailable")
        return response.text

    return result
