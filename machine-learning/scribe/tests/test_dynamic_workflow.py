"""Exercise native plugin delegation with isolated model conversations."""

import json
import sys
import unittest
from collections.abc import AsyncIterator
from unittest.mock import patch

import httpx
from galadril_plugins.law.instruction import Ground, _investigate
from galadril_scribe.contracts import RunRequest, Settings
from galadril_scribe.execution import safe_to_replay
from galadril_scribe.runtime import Runtime
from galadril_scribe.teams import AgentDefinition, TeamDefinition
from galadril_scribe.workflows import TaskGraph, TaskResult, TaskSpec
from pydantic import JsonValue
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.usage import RunUsage, UsageLimits


class DynamicWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_additional_evidence_never_widens_the_authorized_source_scope(
        self,
    ) -> None:
        async def storage(key: str, value: JsonValue | None) -> JsonValue:
            return None

        async def execute(
            spec: TaskSpec,
            inputs: tuple[TaskResult, ...],
            limits: UsageLimits,
            usage: RunUsage,
        ) -> str:
            if spec.evidence:
                return json.dumps({"analysis": "Verified authorized exhibit"})
            return json.dumps(
                {
                    "analysis": "An exhibit is missing",
                    "requested_evidence": [0],
                    "access_reason": "Verify the notification date",
                }
            )

        ground = Ground(
            id="m0",
            question="Question",
            cause="legalite_externe",
            branches=("procedure",),
            passages=("Original",),
        )
        for authorized in ((), (0,)):
            graph = TaskGraph(
                execute, storage, RunUsage(), UsageLimits(request_limit=100)
            )
            if not authorized:
                with self.assertRaisesRegex(ValueError, "source authorization"):
                    await _investigate(
                        graph,
                        ground,
                        common="",
                        evidence=authorized,
                        prefix="fixture",
                        refinements=1,
                        controls=(),
                    )
            else:
                reports = await _investigate(
                    graph,
                    ground,
                    common="",
                    evidence=authorized,
                    prefix="fixture",
                    refinements=1,
                    controls=(),
                )
                self.assertEqual(len(reports), 8)
                self.assertEqual(
                    sum(bool(spec.evidence) for spec in graph.specs.values()), 4
                )

    async def test_full_workflow_does_not_share_reports_with_independent_defenses(
        self,
    ) -> None:
        await self._exercise(refine=False)

    async def test_discovery_refines_one_ground_without_sharing_internal_reports(
        self,
    ) -> None:
        await self._exercise(refine=True)

    async def _exercise(self, *, refine: bool) -> None:
        seen: list[str] = []
        errors: list[BaseException] = []

        async def model(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            instructions = info.instructions or ""
            context = str(messages)
            tools = {item.name for item in info.function_tools}
            if "Task role:" in instructions:
                self.assertIn("Raisonnement et vocabulaire", instructions)
                self.assertNotIn("PRIVATE_HISTORY", context)
                self.assertNotIn("PRIVATE_PROJECT", context)
                self.assertNotIn("instruct_litigation", " ".join(tools))
                if "Identifie exclusivement les moyens" in context:
                    return ModelResponse(
                        parts=[
                            TextPart(
                                json.dumps(
                                    {
                                        "grounds": [
                                            {
                                                "id": f"m{i}",
                                                "question": f"Question {i}",
                                                "cause": "legalite_externe",
                                                "branches": ["procedure"],
                                                "passages": ["Texte original."],
                                                "evidence": [],
                                            }
                                            for i in range(3)
                                        ]
                                    }
                                )
                            )
                        ]
                    )
                role = instructions.split("Task role: ")[1].split("\n")[0]
                seen.append(role)
                if role in {"procedure", "sources", "rejet"}:
                    self.assertIn("DRAFT_PUBLIC", context)
                    self.assertNotIn("PRIVATE_REPORT", context)
                    self.assertIn("web_search", tools)
                    self.assertIn("search_official_law", tools)
                    self.assertNotIn("document_search", tools)
                    self.assertNotIn("search_conversation_history", tools)
                    return ModelResponse(
                        parts=[TextPart("Independent defense")]
                    )
                if "Rédaction contentieuse" in instructions:
                    return ModelResponse(parts=[TextPart("DRAFT_PUBLIC")])
                if "Vérification finale distincte" in context:
                    return ModelResponse(
                        parts=[TextPart("Human validation required")]
                    )
                self.assertNotIn("PRIVATE_REPORT", context)
                if (
                    refine
                    and role == "normes"
                    and "Question actuelle : Question 0" in context
                ):
                    return ModelResponse(
                        parts=[
                            TextPart(
                                json.dumps(
                                    {
                                        "analysis": "PRIVATE_REPORT",
                                        "follow_up_question": "Question reformulée",
                                    }
                                )
                            )
                        ]
                    )
                return ModelResponse(
                    parts=[TextPart(json.dumps({"analysis": "PRIVATE_REPORT"}))]
                )
            self.assertIn("PRIVATE_HISTORY", context)
            tool = "law_algorithm_dossier_instruct_litigation"
            self.assertIn(tool, tools)
            if "human_validation_required" in context:
                return ModelResponse(
                    parts=[TextPart("Draft ready for human review")]
                )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool,
                        {
                            "memorandum": "Texte original.",
                            "refinements": int(refine),
                        },
                    )
                ]
            )

        async def stream(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            response = await model(messages, info)
            for part in response.parts:
                if isinstance(part, TextPart):
                    yield part.content
                elif isinstance(part, ToolCallPart):
                    yield {
                        0: DeltaToolCall(
                            name=part.tool_name,
                            json_args=json.dumps(part.args_as_dict()),
                            tool_call_id="call-workflow",
                        )
                    }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            runtime = Runtime(
                Settings(
                    gateway_tools_url="http://gateway/internal/chat/tools",
                    models={
                        "test": {
                            "provider": "vllm",
                            "model": "fixture",
                            "base_url": "http://model/v1",
                        }
                    },
                    default_model="test",
                ),
                http,
                model=FunctionModel(model, stream_function=stream),
            )
            replayable = runtime.extensions.replayable(
                ("law.algorithm.dossier", "law.mcp.research")
            )
            self.assertIn("law_mcp_research_search_official_law", replayable)
            self.assertIn(
                "law_algorithm_dossier_instruct_litigation", replayable
            )
            self.assertNotIn("law_mcp_research_delete_document", replayable)
            pending = [
                ModelResponse(
                    parts=[
                        ToolCallPart("law_mcp_research_search_official_law", {})
                    ]
                )
            ]
            self.assertFalse(safe_to_replay(pending))
            self.assertTrue(
                safe_to_replay(pending, replayable_tools=replayable)
            )
            request = RunRequest(
                prompt="Instruis ce mémoire",
                capability="a" * 64,
                execution_mode="research",
                execution_context="law.administratif",
                web_search=True,
                tool_ids=("law.algorithm.dossier", "law.mcp.research"),
                history=({"role": "user", "content": "PRIVATE_HISTORY"},),
                team=TeamDefinition(
                    id="user.team.case",
                    name="Law",
                    agents=(
                        AgentDefinition(
                            id="selected",
                            mission="PRIVATE_PROJECT",
                            skill_ids=("law.skill.faits",),
                        ),
                    ),
                ),
            )

            def fail_on_runtime_error(event: str, **fields: object) -> None:
                if event == "scribe.run.failed":
                    error = sys.exception()
                    if error is not None:
                        errors.append(error)

            with patch(
                "galadril_scribe.runtime.logger.warning",
                side_effect=fail_on_runtime_error,
            ):
                chunks = [chunk async for chunk in runtime.stream(request)]
            if errors:
                raise errors[0]
        self.assertEqual(chunks[-1].kind, "completed")
        self.assertEqual(
            sum(
                role in {"normes", "jurisprudence", "preuves", "objections"}
                for role in seen
            ),
            16 if refine else 12,
        )
        self.assertEqual(
            set(seen) & {"procedure", "sources", "rejet"},
            {"procedure", "sources", "rejet"},
        )


if __name__ == "__main__":
    unittest.main()
