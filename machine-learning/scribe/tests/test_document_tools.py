"""Document tools expose typed content without deployment or identity selectors."""

import json
import unittest
from types import SimpleNamespace
from typing import cast

from galadril_plugins.document_templates import TemplateDraft
from galadril_scribe.dependencies import Dependencies
from galadril_scribe.document_tools import (
    Compilation,
    DocumentCase,
    TemplateRequest,
    document_tools,
)
from galadril_scribe.runtime import ToolFailure
from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_ai import RunContext


class DocumentToolsTests(unittest.TestCase):
    def test_unknown_authority_and_storage_fields_are_rejected(self) -> None:
        for field in ("tenant", "owner", "bucket", "command", "endpoint"):
            with self.assertRaises(ValidationError):
                Compilation.model_validate(
                    {
                        "documents": [
                            {"template_id": "a" * 32, "version": "b" * 64}
                        ],
                        field: "other",
                    }
                )
        for path in ("../other.cls", "/etc/passwd", "a\\b.cls", "a//b.cls"):
            with self.assertRaises(ValidationError):
                TemplateDraft.model_validate(
                    {
                        "name": "Template",
                        "entrypoint": path,
                        "assets": [{"path": path, "data": "class"}],
                    }
                )

    def test_sources_are_encoded_without_interpreting_latex(self) -> None:
        draft = TemplateDraft.model_validate(
            {
                "name": "Letter",
                "entrypoint": "main.tex",
                "assets": [
                    {
                        "path": "main.tex",
                        "data": "\\newcommand{\\name}[1]{{#1}}",
                    }
                ],
            }
        )
        self.assertEqual(
            draft.wire()["assets"],
            [
                {
                    "path": "main.tex",
                    "data": list(b"\\newcommand{\\name}[1]{{#1}}"),
                }
            ],
        )
        with self.assertRaises(ValidationError):
            TypeAdapter(TemplateRequest).validate_python(
                {"action": "execute", "command": "sh"}
            )

    def test_native_schemas_never_expose_delegation_or_endpoints(self) -> None:
        async def invoke(*args: object) -> str:
            return "{}"

        tools = document_tools(invoke)
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "redact_document",
                "read_document",
                "compile_document",
                "document_templates",
                "document_job",
            },
        )
        for tool in tools:
            schema = str(tool.tool_def.parameters_json_schema)
            for field in ("capability", "s3_endpoint", "turso", "credentials"):
                self.assertNotIn(field, schema)


class DocumentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_denial_does_not_abort_the_agent(self) -> None:
        async def denied(*args: object) -> str:
            raise ToolFailure("Authorization refused")

        selected = next(
            tool
            for tool in document_tools(invoke=denied)
            if tool.name == "document_templates"
        )
        context = cast(
            RunContext[Dependencies], SimpleNamespace(deps=SimpleNamespace())
        )
        result = await selected.function(
            context,
            TypeAdapter(TemplateRequest).validate_python({"action": "list"}),
        )
        self.assertEqual(json.loads(result)["status"], "unavailable")
        self.assertNotIn("Authorization refused", result)

    async def test_document_case_update_requires_interactive_confirmation(
        self,
    ) -> None:
        async def invoke(*args: object) -> str:
            self.fail("An unconfirmed case update reached Gateway")

        context = cast(
            RunContext[Dependencies],
            SimpleNamespace(deps=SimpleNamespace(answerer=None)),
        )
        tool = next(
            tool
            for tool in document_tools(invoke)
            if tool.name == "document_templates"
        )
        response = await tool.function(
            context,
            DocumentCase(
                action="document_case",
                template_id="a" * 32,
                fields={"administration_name": "Fictional Administration"},
            ),
        )
        self.assertEqual(
            json.loads(response), {"status": "confirmation_required"}
        )

    async def test_redaction_forwards_only_document_reference_and_rules(
        self,
    ) -> None:
        from galadril_scribe.document_tools import RedactDocument

        calls: list[dict[str, JsonValue]] = []

        async def invoke(
            ctx: RunContext[Dependencies],
            operation: str,
            arguments: dict[str, JsonValue],
        ) -> str:
            self.assertEqual(operation, "redact_document")
            calls.append(arguments)
            return '{"download":{"url":"/pengolo/files/result","name":"redacted.pdf"}}'

        tool = next(
            tool
            for tool in document_tools(invoke)
            if tool.name == "redact_document"
        )
        context = cast(
            RunContext[Dependencies], SimpleNamespace(tool_call_id="call")
        )
        result = await tool.function(
            context,
            RedactDocument(
                file_id="a" * 32,
                rules=[{"kind": "regex", "pattern": r"\b\w*sal\w*\b"}],
            ),
        )
        self.assertIn("/pengolo/files/result", result)
        self.assertEqual(calls[0]["file_id"], "a" * 32)
        self.assertNotIn("content_base64", calls[0])

    async def test_multiple_compilations_publish_one_zip_with_stable_request_ids(
        self,
    ) -> None:
        calls: list[tuple[str, dict[str, JsonValue]]] = []

        async def invoke(
            ctx: RunContext[Dependencies],
            operation: str,
            payload: dict[str, JsonValue],
        ) -> str:
            calls.append((operation, payload))
            if operation == "compile_document":
                return json.dumps(
                    {
                        "job_id": "c" * 64
                        if len([item for item in calls if item[0] == operation])
                        % 2
                        else "d" * 64,
                        "status": "queued",
                    }
                )
            if (
                operation == "document_job"
                and payload.get("request", {}).get("action") == "status"
            ):
                return json.dumps({"status": "completed"})
            return json.dumps(
                {"file": {"name": "letters.zip", "expiresAt": 604800000}}
            )

        context = cast(
            RunContext[Dependencies],
            SimpleNamespace(
                deps=SimpleNamespace(capability="private-delegation"),
                tool_call_id="call",
            ),
        )
        compile_tool = next(
            tool
            for tool in document_tools(invoke)
            if tool.name == "compile_document"
        )
        request = Compilation.model_validate(
            {
                "documents": [
                    {"template_id": "a" * 32, "version": "b" * 64},
                    {"template_id": "a" * 32, "version": "e" * 64},
                ],
                "name": "letters.pdf",
            }
        )
        await compile_tool.function(context, request)
        first = [
            payload
            for operation, payload in calls
            if operation == "compile_document"
        ]
        self.assertEqual(
            calls[-1],
            (
                "document_job",
                {
                    "request": {
                        "action": "save_batch",
                        "job_ids": ["c" * 64, "d" * 64],
                        "name": "letters.zip",
                    }
                },
            ),
        )
        calls.clear()
        await compile_tool.function(context, request)
        self.assertEqual(
            first,
            [
                payload
                for operation, payload in calls
                if operation == "compile_document"
            ],
        )


if __name__ == "__main__":
    unittest.main()
