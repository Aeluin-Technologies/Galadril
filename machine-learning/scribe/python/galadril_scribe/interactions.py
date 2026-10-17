"""Generation-scoped answers never become approvals for privileged tool calls."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai_harness.ask_user import (
    AskUserAnswer,
    AskUserRequest,
    AskUserResponse,
    check_response,
)

from galadril_scribe.activity import (
    Identifier,
    QuestionActivity,
    QuestionResolved,
)


class AnswerValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    header: str = Field(min_length=1, max_length=25)
    selected: tuple[Annotated[str, Field(min_length=1, max_length=50)], ...] = (
        Field(default=(), max_length=6)
    )
    custom_answer: str | None = Field(
        default=None, min_length=1, max_length=4096
    )


class AnswerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    question_id: Identifier
    answers: tuple[AnswerValue, ...] = Field(default=(), max_length=10)
    cancelled: bool = False

    @model_validator(mode="after")
    def bounded_submission(self) -> AnswerSubmission:
        if len(self.model_dump_json().encode()) > 65536:
            raise ValueError("Answer exceeds limit")
        return self

    def native(self) -> AskUserResponse:
        return AskUserResponse(
            answers=tuple(
                AskUserAnswer(
                    header=answer.header,
                    selected=answer.selected,
                    custom_answer=answer.custom_answer,
                )
                for answer in self.answers
            ),
            cancelled=self.cancelled,
        )


class InteractionFailure(ValueError):
    """Public error codes distinguish unavailable sessions from rejected answers."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Interaction " + code)


@dataclass(slots=True)
class _Pending:
    request: AskUserRequest
    future: asyncio.Future[AskUserResponse]


@dataclass(slots=True)
class _Session:
    key: str = field(repr=False)
    pending: dict[str, _Pending] = field(default_factory=dict)
    answered: dict[str, AnswerSubmission] = field(default_factory=dict)


class InteractionBroker:
    """Retains only bounded live questions until the owning execution ends."""

    __slots__ = ("_sessions",)

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    def open(self, identifier: str, key: str) -> None:
        if identifier in self._sessions or len(self._sessions) >= 32:
            raise InteractionFailure("unavailable")
        self._sessions[identifier] = _Session(key)

    def close(self, identifier: str) -> None:
        session = self._sessions.pop(identifier, None)
        if session is not None:
            for pending in session.pending.values():
                pending.future.cancel()

    async def ask(
        self,
        identifier: str,
        request: AskUserRequest,
        emit: Callable[[str], Awaitable[None]],
    ) -> AskUserResponse:
        session = self._sessions.get(identifier)
        if (
            session is None
            or request.id in session.pending
            or request.id in session.answered
            or len(session.pending) + len(session.answered) >= 32
        ):
            raise InteractionFailure("unavailable")
        future: asyncio.Future[AskUserResponse] = (
            asyncio.get_running_loop().create_future()
        )
        session.pending[request.id] = _Pending(request, future)
        cancelled = True
        resolved = False
        try:
            activity = QuestionActivity(
                id=request.id, questions=request.questions
            )
            await emit(activity.model_dump_json())
            response = await future
            cancelled = response.cancelled
            resolved = True
            return response
        finally:
            session.pending.pop(request.id, None)
            if resolved:
                await emit(
                    QuestionResolved(
                        id=request.id, cancelled=cancelled
                    ).model_dump_json()
                )

    def answer(
        self, identifier: str, key: str, submission: AnswerSubmission
    ) -> None:
        session = self._sessions.get(identifier)
        if session is None or not hmac.compare_digest(session.key, key):
            raise InteractionFailure("unavailable")
        previous = session.answered.get(submission.question_id)
        if previous is not None:
            if previous != submission:
                raise InteractionFailure("conflict")
            return
        pending = session.pending.get(submission.question_id)
        if pending is None or pending.future.done():
            raise InteractionFailure("unavailable")
        try:
            response = submission.native()
            check_response(pending.request, response)
        except ValueError:
            raise InteractionFailure("invalid") from None
        session.answered[submission.question_id] = submission
        pending.future.set_result(response)
