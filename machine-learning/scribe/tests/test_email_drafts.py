"""Email drafting reuses Scribe aliases and Skills without mail-send authority."""

import asyncio
import unittest

import httpx
from galadril_scribe.contracts import Settings
from galadril_scribe.email import DraftRequest, generate_draft
from galadril_scribe.runtime import Runtime
from pydantic_ai.models.test import TestModel


class EmailDrafting(unittest.TestCase):
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

    def test_unknown_skills_fail_before_inference(self) -> None:
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
                    model=TestModel(),
                )
                with self.assertRaises(ValueError):
                    await generate_draft(
                        runtime,
                        DraftRequest(
                            model="local",
                            prompt="Reply",
                            skill_ids=("unknown.skill.x",),
                            message="Hello",
                        ),
                    )
                self.assertEqual(runtime.active, 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
