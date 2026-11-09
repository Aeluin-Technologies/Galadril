"""Email drafting reuses Scribe aliases and Skills without mail-send authority."""

import asyncio
import unittest
from unittest.mock import patch

import httpx
from galadril_scribe.contracts import Settings
from galadril_scribe.email import DraftRequest, generate_draft
from galadril_scribe.runtime import Runtime
from pydantic_ai.models.test import TestModel


class EmailDrafting(unittest.TestCase):
    def test_native_mail_read_follows_the_original_body_cursor(self) -> None:
        import json
        from types import SimpleNamespace

        from galadril_scribe.email import read_email
        from pydantic_ai import ModelRetry

        async def run() -> None:
            calls = 0

            def gateway(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                self.assertEqual(
                    json.loads(request.content)["request"]["offset"], 16384
                )
                return httpx.Response(
                    200,
                    json={"body": "End of original message.", "next_offset": 0},
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(gateway)
            ) as client:
                ctx = SimpleNamespace(
                    deps=SimpleNamespace(
                        client=client,
                        capability="delegated",
                        tools_url="http://gateway/tools",
                        workspace_mcp_url=None,
                        workspace_mcp_resource="https://mcp.example/workspace/mcp",
                    )
                )
                result = json.loads(
                    await read_email(ctx, "a" * 32, "b" * 32, offset=16384)
                )
                self.assertEqual(
                    result["message"]["body"], "End of original message."
                )
                self.assertEqual(result["message"]["next_offset"], 0)
                with self.assertRaises(ModelRetry):
                    await read_email(ctx, "a" * 32, "b" * 32, offset=-1)
            self.assertEqual(calls, 1)

        asyncio.run(run())

    def test_native_mail_search_reads_original_and_keeps_reply_parent(
        self,
    ) -> None:
        import json
        from types import SimpleNamespace

        from galadril_scribe.email import read_email, search_emails

        async def run() -> None:
            operations: list[str] = []
            account, message = "a" * 32, "b" * 32

            def gateway(request: httpx.Request) -> httpx.Response:
                self.assertEqual(
                    request.headers["authorization"], "Bearer delegated"
                )
                payload = json.loads(request.content)
                self.assertEqual(payload["operation"], "email")
                action = payload["request"]
                operations.append(action["operation"])
                if action["operation"] == "accounts":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "id": account,
                                "rights": ["read"],
                                "config": {"address": "owner@example.org"},
                            }
                        ],
                    )
                if action["operation"] == "messages":
                    self.assertEqual(
                        action,
                        {
                            "operation": "messages",
                            "account": account,
                            "query": "Alex",
                            "before": 0,
                        },
                    )
                    return httpx.Response(
                        200,
                        json={
                            "items": [
                                {
                                    "id": message,
                                    "message": {
                                        "sender": "alex@example.org",
                                        "subject": "Meeting",
                                    },
                                }
                            ],
                            "next_before": 0,
                        },
                    )
                self.assertEqual(
                    action,
                    {
                        "operation": "message",
                        "account": account,
                        "message": message,
                        "offset": 0,
                    },
                )
                return httpx.Response(
                    200,
                    json={
                        "sender": "alex@example.org",
                        "text": "Complete original message",
                        "subject": "Meeting",
                    },
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(gateway)
            ) as client:
                ctx = SimpleNamespace(
                    deps=SimpleNamespace(
                        client=client,
                        capability="delegated",
                        tools_url="http://gateway/internal/chat/tools",
                        workspace_mcp_url=None,
                        workspace_mcp_resource="https://mcp.example/workspace/mcp",
                    )
                )
                result = json.loads(await search_emails(ctx, "Alex"))
                self.assertEqual(result["account"], account)
                result = json.loads(await read_email(ctx, account, message))
                self.assertEqual(
                    result["message"]["text"], "Complete original message"
                )
                self.assertEqual(
                    result["reply_source"],
                    {"account": account, "message": message},
                )
            self.assertEqual(operations, ["accounts", "messages", "message"])

        asyncio.run(run())

    def test_native_mail_tools_report_denial_without_claiming_a_quota(
        self,
    ) -> None:
        import json
        from types import SimpleNamespace

        from galadril_scribe.email import read_email

        async def run() -> None:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(403)
                )
            ) as client:
                ctx = SimpleNamespace(
                    deps=SimpleNamespace(
                        client=client,
                        capability="delegated",
                        tools_url="http://gateway/internal/chat/tools",
                        workspace_mcp_url=None,
                        workspace_mcp_resource="https://mcp.example/workspace/mcp",
                    )
                )
                result = json.loads(await read_email(ctx, "a" * 32, "b" * 32))
                self.assertEqual(result["status"], "denied")
                self.assertEqual(result["operation"], "message")
                self.assertNotIn("quota", json.dumps(result))

        asyncio.run(run())

    def test_model_decision_can_return_no_reply_without_a_body(self) -> None:
        async def run() -> None:
            async with httpx.AsyncClient() as client:
                runtime = Runtime(
                    Settings(
                        gateway_tools_url="http://gateway/internal/chat/tools",
                        models={
                            "local": {
                                "model": "local",
                                "base_url": "http://local/v1",
                            }
                        },
                        default_model="local",
                    ),
                    client,
                    model=TestModel(
                        custom_output_args={
                            "should_reply": False,
                            "body": "",
                            "summary": "Information only",
                            "category": "information",
                        }
                    ),
                )
                result = await generate_draft(
                    runtime,
                    DraftRequest(
                        model="local",
                        prompt="Do not answer informational mail.",
                        message="For your information.",
                        allow_no_reply=True,
                    ),
                )
                self.assertFalse(result.should_reply)
                self.assertEqual(result.body, "")
                self.assertEqual(runtime.active, 0)

        asyncio.run(run())

    def test_attachment_parse_route_requires_private_key_and_never_embeds(
        self,
    ) -> None:
        import base64
        import hashlib
        import os
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from fastapi import FastAPI
        from galadril_scribe.email import install_email_routes
        from galadril_scribe.retrieval_contracts import ParsedDocument, TextUnit

        async def run() -> None:
            documents = SimpleNamespace(
                parse=AsyncMock(
                    return_value=ParsedDocument(
                        units=(
                            TextUnit(
                                ordinal=0, text="Evidence", start=0, end=8
                            ),
                        )
                    )
                ),
                embed=AsyncMock(),
            )
            runtime = SimpleNamespace(documents=documents)
            with tempfile.TemporaryDirectory() as directory:
                key = Path(directory) / "key"
                key.write_text("k" * 48)
                app = FastAPI()
                with patch.dict(
                    os.environ, {"UTILS_SERVICE_KEY_FILE": str(key)}
                ):
                    install_email_routes(app, runtime)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://scribe",
                ) as client:
                    payload = {
                        "mime": "application/pdf",
                        "content": base64.b64encode(b"%PDF-test").decode(),
                        "checksum": hashlib.sha256(b"%PDF-test").hexdigest(),
                    }
                    denied = await client.post("/utils/parse", json=payload)
                    self.assertEqual(denied.status_code, 403)
                    documents.parse.assert_not_awaited()
                    allowed = await client.post(
                        "/utils/parse",
                        json=payload,
                        headers={"x-utils-key": "k" * 48},
                    )
                    self.assertEqual(allowed.status_code, 200)
                    self.assertEqual(
                        allowed.json()["units"][0]["text"], "Evidence"
                    )
                    documents.embed.assert_not_awaited()

        asyncio.run(run())

    def test_reply_proposal_retains_authorized_source_without_selecting_sender(
        self,
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from galadril_plugins.utils.models import ReplySource
        from galadril_scribe.activity import activity_adapter
        from galadril_scribe.email import propose_email

        async def run() -> None:
            activity = AsyncMock()
            source = {"account": "a" * 32, "message": "b" * 32}

            def gateway(request: httpx.Request) -> httpx.Response:
                import json

                self.assertEqual(
                    json.loads(request.content)["request"],
                    {"operation": "message", **source},
                )
                return httpx.Response(
                    200,
                    json={
                        "sender": "sender@example.org",
                        "subject": "Question",
                    },
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(gateway)
            ) as client:
                ctx = SimpleNamespace(
                    deps=SimpleNamespace(
                        activity=activity,
                        client=client,
                        tools_url="http://gateway/tools",
                        workspace_mcp_url=None,
                        workspace_mcp_resource="https://mcp.example/workspace/mcp",
                        capability="private",
                    )
                )
                await propose_email(
                    ctx,
                    to=[],
                    subject="Re: Question",
                    body="Reply",
                    reply_source=ReplySource.model_validate(source),
                )
            proposal = activity_adapter.validate_json(
                activity.await_args.args[0]
            )
            self.assertEqual(proposal.reply_source.model_dump(), source)
            self.assertEqual(proposal.to, ("sender@example.org",))

        asyncio.run(run())

    def test_a_more_precise_contact_answer_is_searched_before_drafting(
        self,
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from galadril_plugins.utils.contacts import Contact, ContactPage
        from galadril_scribe.contacts import resolve_recipient
        from pydantic_ai_harness.ask_user import AskUserAnswer, AskUserResponse

        async def run() -> None:
            answerer = AsyncMock(
                return_value=AskUserResponse(
                    answers=(
                        AskUserAnswer(
                            header="Email recipient",
                            custom_answer="Jean-Daniel Durand",
                        ),
                    )
                )
            )
            context = SimpleNamespace(deps=SimpleNamespace(answerer=answerer))
            contact = Contact(
                name="Jean-Daniel Durand", email="jean@example.org"
            )
            lookup = AsyncMock(
                side_effect=[
                    ContactPage(items=(contact,), more=True),
                    ContactPage(items=(contact,)),
                ]
            )
            with patch("galadril_scribe.contacts.contact_page", new=lookup):
                self.assertEqual(
                    await resolve_recipient(context, "Jean"), "jean@example.org"
                )
            self.assertEqual(
                [call.args[1] for call in lookup.await_args_list],
                ["Jean", "Jean-Daniel Durand"],
            )

        asyncio.run(run())

    def test_cancelled_or_incomplete_contact_selection_never_creates_a_draft(
        self,
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from galadril_plugins.utils.contacts import Contact, ContactPage
        from galadril_scribe.email import propose_email
        from pydantic_ai import ModelRetry
        from pydantic_ai_harness.ask_user import AskUserResponse

        async def run() -> None:
            activity = AsyncMock()
            answerer = AsyncMock(return_value=AskUserResponse(cancelled=True))
            context = SimpleNamespace(
                deps=SimpleNamespace(activity=activity, answerer=answerer)
            )
            page = ContactPage(
                items=(Contact(name="Jean", email="jean@example.org"),),
                incomplete=True,
            )
            with patch(
                "galadril_scribe.contacts.contact_page",
                new=AsyncMock(return_value=page),
            ):
                with self.assertRaises(ModelRetry):
                    await propose_email(
                        context, to=["Jean"], subject="Hello", body="Hello"
                    )
            answerer.assert_awaited_once()
            activity.assert_not_awaited()

        asyncio.run(run())

    def test_contact_name_is_resolved_before_emitting_a_reviewable_draft(
        self,
    ) -> None:
        from types import SimpleNamespace

        from galadril_scribe.activity import activity_adapter
        from galadril_scribe.email import propose_email

        async def run() -> None:
            emitted: list[str] = []

            async def emit(value: str) -> None:
                emitted.append(value)

            def gateway(request: httpx.Request) -> httpx.Response:
                import json

                self.assertEqual(
                    json.loads(request.content),
                    {
                        "operation": "email",
                        "request": {
                            "operation": "contacts",
                            "query": "Jean-Daniel",
                        },
                    },
                )
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "name": "Jean-Daniel Durand",
                                "email": "jean-daniel@example.org",
                                "source": "zimbra",
                            }
                        ],
                        "more": False,
                        "incomplete": False,
                    },
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(gateway)
            ) as client:
                ctx = SimpleNamespace(
                    deps=SimpleNamespace(
                        activity=emit,
                        client=client,
                        tools_url="http://gateway/tools",
                        workspace_mcp_url=None,
                        workspace_mcp_resource="https://mcp.example/workspace/mcp",
                        capability="private",
                        answerer=None,
                    )
                )
                await propose_email(
                    ctx,
                    to=["Jean-Daniel"],
                    subject="Bonjour",
                    body="Pour validation",
                )
            self.assertEqual(
                activity_adapter.validate_json(emitted[0]).to,
                ("jean-daniel@example.org",),
            )

        asyncio.run(run())

    def test_ambiguous_contacts_require_a_generation_scoped_user_choice(
        self,
    ) -> None:
        from types import SimpleNamespace

        from galadril_scribe.activity import activity_adapter
        from galadril_scribe.email import propose_email
        from pydantic_ai_harness.ask_user import (
            AskUserAnswer,
            AskUserRequest,
            AskUserResponse,
        )

        async def run() -> None:
            emitted: list[str] = []
            asked: list[AskUserRequest] = []

            async def emit(value: str) -> None:
                emitted.append(value)

            async def answer(request: AskUserRequest) -> AskUserResponse:
                self.assertEqual(emitted, [])
                asked.append(request)
                question = request.questions[0]
                return AskUserResponse(
                    answers=(
                        AskUserAnswer(
                            header=question.header,
                            selected=(question.options[1].label,),
                        ),
                    )
                )

            def gateway(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "name": "Jean-Daniel Durand",
                                "email": "durand@example.org",
                                "source": "zimbra",
                            },
                            {
                                "name": "Jean-Daniel Martin",
                                "email": "martin@example.org",
                                "source": "zimbra",
                            },
                        ],
                        "more": False,
                        "incomplete": False,
                    },
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(gateway)
            ) as client:
                ctx = SimpleNamespace(
                    deps=SimpleNamespace(
                        activity=emit,
                        client=client,
                        tools_url="http://gateway/tools",
                        workspace_mcp_url=None,
                        workspace_mcp_resource="https://mcp.example/workspace/mcp",
                        capability="private",
                        answerer=answer,
                    )
                )
                await propose_email(
                    ctx,
                    to=["Jean-Daniel"],
                    subject="Bonjour",
                    body="Pour validation",
                )
            self.assertEqual(len(asked), 1)
            self.assertEqual(
                activity_adapter.validate_json(emitted[0]).to,
                ("martin@example.org",),
            )

        asyncio.run(run())

    def test_plugin_draft_tool_emits_a_card_without_accessing_an_account(
        self,
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from galadril_plugins.utils.tools import tools
        from galadril_scribe.activity import activity_adapter

        async def run() -> None:
            emitted: list[str] = []

            async def emit(value: str) -> None:
                emitted.append(value)

            client = AsyncMock()
            ctx = SimpleNamespace(
                deps=SimpleNamespace(
                    activity=emit,
                    client=client,
                    tools_url="http://gateway/internal/chat/tools",
                    workspace_mcp_url=None,
                    workspace_mcp_resource="https://mcp.example/workspace/mcp",
                    capability="private",
                )
            )
            function = tools().tools["email"].function
            await function(
                ctx,
                action="save_draft",
                account="opaque-account",
                recipient="recipient@example.org",
                subject="Hello",
                body="For review",
            )
            client.post.assert_not_awaited()
            self.assertEqual(len(emitted), 1)
            activity = activity_adapter.validate_json(emitted[0])
            self.assertEqual(activity.type, "email_draft")
            self.assertNotIn("opaque-account", emitted[0])

        asyncio.run(run())

    def test_chat_proposal_has_no_sender_or_account_authority(self) -> None:
        from types import SimpleNamespace

        from galadril_scribe.activity import activity_adapter
        from galadril_scribe.email import propose_email

        async def run() -> None:
            emitted = []

            async def emit(value: str) -> None:
                emitted.append(value)

            ctx = SimpleNamespace(deps=SimpleNamespace(activity=emit))
            result = await propose_email(
                ctx,
                to=["recipient@example.org"],
                subject="Hello",
                body="A draft",
                cc=[],
                bcc=["private@example.org"],
            )
            self.assertEqual(len(emitted), 1)
            activity = activity_adapter.validate_json(emitted[0])
            self.assertEqual(activity.type, "email_draft")
            self.assertNotIn("account", emitted[0])
            self.assertNotIn("sender", emitted[0])
            self.assertNotIn("private@example.org", result)
            with self.assertRaises(ValueError):
                await propose_email(
                    ctx,
                    to=["recipient@example.org"],
                    subject="Hello",
                    body="🦊" * 16384,
                )
            self.assertEqual(len(emitted), 1)

        asyncio.run(run())

    def test_removed_primary_automatically_selects_an_available_equivalent(
        self,
    ) -> None:
        async def run() -> None:
            async with httpx.AsyncClient() as client:
                runtime = Runtime(
                    Settings(
                        gateway_tools_url="http://gateway/internal/chat/tools",
                        models={
                            "local": {
                                "model": "local",
                                "base_url": "http://local/v1",
                            },
                            "api": {
                                "model": "api",
                                "base_url": "http://remote/v1",
                            },
                        },
                        default_model="local",
                    ),
                    client,
                    model=TestModel(
                        custom_output_args={
                            "body": "A safe reply",
                            "summary": "Question about delivery",
                            "category": "support",
                        }
                    ),
                )
                runtime.models.pop("local")
                result = await generate_draft(
                    runtime,
                    DraftRequest(
                        model="local",
                        prompt="Reply politely",
                        skill_ids=(),
                        message="When will this ship?",
                    ),
                )
                self.assertEqual(result.model, "api")
                self.assertEqual(result.body, "A safe reply")
                self.assertEqual(runtime.active, 0)
                runtime.models["local"] = runtime.models["api"]
                runtime.settings.models.pop("local")
                runtime.settings.default_model = "api"
                result = await generate_draft(
                    runtime,
                    DraftRequest(
                        model="local",
                        prompt="Reply",
                        message="Hello",
                    ),
                )
                self.assertEqual(result.model, "api")
                runtime.models.clear()
                with self.assertRaises(ValueError):
                    await generate_draft(
                        runtime,
                        DraftRequest(
                            model="missing",
                            fallbacks=(),
                            prompt="Reply",
                            skill_ids=(),
                            message="Hello",
                        ),
                    )

        asyncio.run(run())

    def test_draft_never_loads_workflow_skills(self) -> None:
        async def run() -> None:
            async with httpx.AsyncClient() as client:
                runtime = Runtime(
                    Settings(
                        gateway_tools_url="http://gateway/internal/chat/tools",
                        models={
                            "local": {
                                "model": "local",
                                "base_url": "http://local/v1",
                            }
                        },
                        default_model="local",
                    ),
                    client,
                    model=TestModel(
                        custom_output_args={
                            "body": "Confirmed.",
                            "summary": "Confirmation",
                            "category": "mail",
                        }
                    ),
                )
                with patch.object(
                    type(runtime.teams),
                    "skills",
                    side_effect=AssertionError(
                        "Email drafting loaded a workflow Skill"
                    ),
                ):
                    result = await generate_draft(
                        runtime,
                        DraftRequest(
                            model="local",
                            prompt="Reply",
                            skill_ids=("unknown.skill.x",),
                            message="Hello",
                        ),
                    )
                self.assertEqual(result.body, "Confirmed.")
                self.assertEqual(runtime.active, 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
