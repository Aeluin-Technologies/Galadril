"""Native harness components must work with the exact SDK pinned by Scribe."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness import (
    AskUser,
    GuardrailResult,
    OutputGuardrail,
    Planning,
)
from pydantic_ai_harness.ask_user import AskUserRequest, AskUserResponse


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_native_planning_and_output_guardrails_compose() -> None:
    def redact(output: object) -> GuardrailResult:
        return (
            GuardrailResult.replace("Redacted")
            if output == "PRIVATE_VALUE"
            else GuardrailResult.allow()
        )

    agent = Agent(
        TestModel(call_tools=[], custom_output_text="PRIVATE_VALUE"),
        capabilities=[Planning(), OutputGuardrail(guard=redact)],
    )
    result = await agent.run("Produce a plan")
    assert result.output == "Redacted"


@pytest.mark.anyio
async def test_native_ask_user_waits_for_supplied_answerer() -> None:
    seen: list[str] = []

    async def answer(request: AskUserRequest) -> AskUserResponse:
        seen.append(request.id)
        return AskUserResponse(cancelled=True)

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        if any(
            isinstance(part, ToolReturnPart)
            for message in messages
            for part in message.parts
        ):
            return ModelResponse(parts=[TextPart("Question resolved")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "ask_user_question",
                    {
                        "questions": [
                            {
                                "header": "Sources",
                                "question": "Which sources?",
                                "options": [
                                    {"label": "Official"},
                                    {"label": "All public"},
                                ],
                            }
                        ]
                    },
                )
            ]
        )

    agent = Agent(
        FunctionModel(response),
        capabilities=[AskUser(answerer=answer, timeout=1)],
    )
    result = await agent.run("Ask me a question")
    assert isinstance(result.output, str)
    assert len(seen) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
