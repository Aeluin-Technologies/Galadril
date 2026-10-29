"""Email pipeline extraction preserves source identity and real message time."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from galadril_pipeline.config import PipelineStep, Source
from galadril_pipeline.events import PipelineCommand
from galadril_plugins import PluginRegistry, PluginScope
from galadril_plugins.utils.pipeline import email_participants, email_reply


class EmailReplyTransport(unittest.IsolatedAsyncioTestCase):
    async def test_ambient_proxy_cannot_receive_the_service_credential(
        self,
    ) -> None:
        destination_requests: list[bytes] = []
        proxy_requests: list[bytes] = []

        async def respond(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
            requests: list[bytes],
        ) -> None:
            try:
                requests.append(await reader.readuntil(b"\r\n\r\n"))
                body = b'{"status":"queued"}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async def destination(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await respond(reader, writer, destination_requests)

        async def proxy(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await respond(reader, writer, proxy_requests)

        async with (
            await asyncio.start_server(destination, "127.0.0.1", 0) as target,
            await asyncio.start_server(proxy, "127.0.0.1", 0) as relay,
            asyncio.timeout(5),
        ):
            target_port = target.sockets[0].getsockname()[1]
            proxy_port = relay.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                key_file = Path(directory) / "service-key"
                key_file.write_text("k" * 48)
                with patch.dict(
                    os.environ,
                    {
                        "UTILS_SERVICE_URL": f"http://127.0.0.1:{target_port}",
                        "UTILS_SERVICE_KEY_FILE": str(key_file),
                        "HTTP_PROXY": f"http://127.0.0.1:{proxy_port}",
                        "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
                        "ALL_PROXY": f"http://127.0.0.1:{proxy_port}",
                        "NO_PROXY": "",
                        "no_proxy": "",
                        "http_proxy": f"http://127.0.0.1:{proxy_port}",
                    },
                ):
                    step = PipelineStep(
                        step="reply",
                        type="algorithm",
                        capability_id="utils.algorithm.email",
                        capability_version="1.0.0",
                    )
                    scope = PluginScope("tenant_a", "mail", "a" * 32)
                    command = PipelineCommand(
                        correlation_id=uuid4(),
                        pipeline="mail",
                        tenant_id="tenant_a",
                        step="reply",
                        step_type="algorithm",
                        resource_class="cpu",
                        payload={
                            "record": {
                                "raw_payload": {
                                    "metadata": {
                                        "source_kind": "email",
                                        "account_id": "a" * 32,
                                        "email_id": "b" * 32,
                                        "message_id": "<one@example.org>",
                                        "sender": "alice@example.org",
                                        "recipients": ["bob@example.org"],
                                    }
                                }
                            }
                        },
                    )
                    result = await email_reply(scope, step).execute(
                        command, step
                    )
            self.assertEqual(result["email_reply"], {"status": "queued"})
            self.assertEqual(proxy_requests, [])
            self.assertEqual(len(destination_requests), 1)
            self.assertIn(
                b"POST /pipeline/reply HTTP/1.1", destination_requests[0]
            )
            self.assertIn(b"x-utils-key: " + b"k" * 48, destination_requests[0])


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


class ReplyTemplates(unittest.TestCase):
    def test_templates_reject_unknown_fields_and_do_not_evaluate_expressions(
        self,
    ) -> None:
        from galadril_plugins.utils.models import ReplyComposition
        from pydantic import ValidationError

        for value in [
            "Hello {{ credentials }}",
            "{{ sender.__class__ }}",
            "{{ sender",
        ]:
            with self.assertRaises(ValidationError):
                ReplyComposition(generation="template", template=value)


class AuthoredLayout(unittest.TestCase):
    def test_mailbox_selection_survives_runtime_source_validation(self) -> None:
        source = Source.model_validate(
            {
                "id": "mail",
                "match_pattern": ".*",
                "schema_path": "email.avsc",
                "account_id": "a" * 32,
            }
        )
        self.assertEqual(source.model_dump()["account_id"], "a" * 32)

    def test_layout_metadata_is_not_interpreted_as_service_configuration(
        self,
    ) -> None:
        registry = PluginRegistry.discover()
        registry.validate_pipeline(
            {
                "name": "Mail",
                "sources": [
                    {
                        "id": "mail",
                        "account_id": "a" * 32,
                        "topic": "raw",
                        "match_pattern": ".*",
                        "schema_path": "schemas/avro/email.avsc",
                        "parser": "json",
                        "source_kind": "email",
                        "capability_id": "utils.input.email",
                        "capability_version": "1.0.0",
                    }
                ],
                "pipeline": [],
                "editor": {"positions": {"mail": {"x": 40, "y": 80}}},
            }
        )


if __name__ == "__main__":
    unittest.main()
