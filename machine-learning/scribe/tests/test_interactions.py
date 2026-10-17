"""Human answers stay bound to the active generation and validated question."""

import asyncio

import pytest
from galadril_scribe.interactions import (
    AnswerSubmission,
    InteractionBroker,
    InteractionFailure,
)
from pydantic_ai_harness.ask_user import AskUserRequest, Question


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def question() -> AskUserRequest:
    return AskUserRequest(
        id="q1",
        questions=(
            Question(
                header="Sources",
                question="Which sources?",
                options=[{"label": "Official"}, {"label": "All public"}],
            ),
        ),
    )


@pytest.mark.anyio
async def test_answer_is_validated_scoped_and_idempotent() -> None:
    broker = InteractionBroker()
    broker.open("a" * 32, "b" * 64)
    events: list[str] = []

    async def emit(content: str) -> None:
        events.append(content)

    pending = asyncio.create_task(broker.ask("a" * 32, question(), emit))
    await asyncio.sleep(0)
    assert '"type":"question"' in events[0]
    answer = AnswerSubmission(
        question_id="q1",
        answers=({"header": "Sources", "selected": ["Official"]},),
    )
    for identifier, key in [("c" * 32, "b" * 64), ("a" * 32, "d" * 64)]:
        with pytest.raises(InteractionFailure):
            broker.answer(identifier, key, answer)
    with pytest.raises(InteractionFailure):
        broker.answer(
            "a" * 32,
            "b" * 64,
            AnswerSubmission(
                question_id="q1",
                answers=({"header": "Sources", "selected": ["Not offered"]},),
            ),
        )
    assert not pending.done()
    broker.answer("a" * 32, "b" * 64, answer)
    broker.answer("a" * 32, "b" * 64, answer)
    response = await pending
    assert response.answers[0].selected == ("Official",)
    assert '"type":"question_resolved"' in events[-1]
    with pytest.raises(InteractionFailure):
        broker.answer(
            "a" * 32,
            "b" * 64,
            AnswerSubmission(question_id="q1", cancelled=True),
        )
    broker.close("a" * 32)
    with pytest.raises(InteractionFailure):
        broker.answer("a" * 32, "b" * 64, answer)


@pytest.mark.anyio
async def test_closing_generation_cancels_waiting_questions() -> None:
    broker = InteractionBroker()
    broker.open("a" * 32, "b" * 64)

    async def emit(content: str) -> None:
        return None

    pending = asyncio.create_task(broker.ask("a" * 32, question(), emit))
    await asyncio.sleep(0)
    broker.close("a" * 32)
    with pytest.raises(asyncio.CancelledError):
        await pending


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
