"""Bounded public progress excludes hidden reasoning and execution authority."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai_harness.ask_user import Question

Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
]


class PhaseActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["phase"] = "phase"
    phase: Literal["thinking", "writing", "tool", "agent", "waiting"]
    agent_id: Identifier | None = None


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Identifier
    content: str = Field(min_length=1, max_length=2048)
    status: Literal[
        "pending", "in_progress", "completed", "cancelled", "blocked"
    ]
    active_form: str = Field(default="", max_length=2048)
    parent_id: Identifier | None = None


class PlanActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["plan"] = "plan"
    operation: Literal["upsert", "delete"] = "upsert"
    agent_id: Identifier | None = None
    task: PlanTask


class QuestionActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["question"] = "question"
    id: Identifier
    questions: tuple[Question, ...] = Field(min_length=1, max_length=10)


class QuestionResolved(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["question_resolved"] = "question_resolved"
    id: Identifier
    cancelled: bool = False


Activity = Annotated[
    PhaseActivity | PlanActivity | QuestionActivity | QuestionResolved,
    Field(discriminator="type"),
]
activity_adapter: TypeAdapter[Activity] = TypeAdapter(Activity)
