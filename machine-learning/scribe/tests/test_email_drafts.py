"""Email drafting reuses Scribe aliases and Skills without mail-send authority."""

import asyncio
import unittest

import httpx
from galadril_scribe.contracts import Settings
from galadril_scribe.email import DraftRequest, generate_draft
from galadril_scribe.runtime import Runtime
from pydantic_ai.models.test import TestModel


class EmailDrafting(unittest.TestCase):
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
