"""Email pipeline extraction preserves source identity and real message time."""

import asyncio
import unittest
from uuid import uuid4

from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_plugins import PluginRegistry, PluginScope
from galadril_plugins.utils.pipeline import email_participants


class EmailPipeline(unittest.TestCase):
    def test_participants_are_stable_per_tenant_and_share_the_message_event(
        self,
    ) -> None:
        scope = PluginScope("tenant_a", "mail", "a" * 32)
        step = PipelineStep(
            step="participants",
            type="algorithm",
            input_from=["mail"],
            capability_id="utils.algorithm.email",
            capability_version="1.0.0",
        )
        metadata = {
            "source_kind": "email",
            "account_id": "a" * 32,
            "email_id": "b" * 32,
            "message_id": "<one@example.org>",
            "thread_id": "thread",
            "sender": "alice@example.org",
            "recipients": ["bob@example.org", "alice@example.org"],
            "subject": "Meeting",
            "date": "Mon, 26 Oct 2026 10:30:00 +0100",
            "references": "",
        }
        command = PipelineCommand(
            correlation_id=uuid4(),
            pipeline="mail",
            tenant_id="tenant_a",
            step="participants",
            step_type="algorithm",
            resource_class="cpu",
            payload={
                "record": {
                    "record_id": "record",
                    "timestamp": "2026-10-27T00:00:00Z",
                    "raw_payload": {"content": "Hello", "metadata": metadata},
                }
            },
        )
        handler = email_participants(scope, step)
        first = asyncio.run(handler.execute(command, step))
        second = asyncio.run(handler.execute(command, step))
        self.assertEqual(first, second)
        self.assertEqual(
            first["record"]["timestamp"], "2026-10-26T09:30:00+00:00"
        )
        self.assertEqual(len(first["data"]), 2)
        self.assertEqual(
            first["record"]["raw_payload"]["metadata"]["message_id"],
            "<one@example.org>",
        )
        self.assertEqual(first["data"][0]["roles"], ["sender", "recipient"])
        other = asyncio.run(
            email_participants(
                PluginScope("tenant_b", "mail", "a" * 32), step
            ).execute(
                command.model_copy(update={"tenant_id": "tenant_b"}), step
            )
        )
        self.assertNotEqual(
            first["data"][0]["resolved_entity_id"],
            other["data"][0]["resolved_entity_id"],
        )

    def test_extraction_is_discoverable_and_does_not_accept_an_injected_header(
        self,
    ) -> None:
        registry = PluginRegistry.discover()
        self.assertTrue(
            any(
                item.id == "utils.algorithm.email"
                for item in registry.catalog.capabilities
            )
        )
        from galadril_plugins.utils.pipeline import EmailMetadata
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            EmailMetadata(
                source_kind="email",
                account_id="a" * 32,
                email_id="b" * 32,
                sender="alice@example.org\nBcc:other@example.org",
                recipients=["bob@example.org"],
                message_id="<one@example.org>",
            )


if __name__ == "__main__":
    unittest.main()
