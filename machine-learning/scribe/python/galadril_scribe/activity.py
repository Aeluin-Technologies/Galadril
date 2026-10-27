"""Bounded public progress excludes hidden reasoning and execution authority."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic_ai_harness.ask_user import Question

Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
]
WarningSource = Literal[
    "search",
    "graph",
    "causal",
    "library",
    "document_search",
    "search_evidence",
    "read_file",
    "attachment",
    "web",
    "run_python",
    "tool",
]


class EmailDraftActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["email_draft"] = "email_draft"
    id: Identifier
    to: tuple[str, ...] = Field(min_length=1, max_length=20)
    cc: tuple[str, ...] = Field(default=(), max_length=20)
    bcc: tuple[str, ...] = Field(default=(), max_length=20)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=16384)

    @model_validator(mode="after")
    def valid_headers(self) -> "EmailDraftActivity":
        if any(
            "@" not in item
            or len(item) > 320
            or any(ord(c) < 32 or ord(c) == 127 for c in item)
            for item in (*self.to, *self.cc, *self.bcc)
        ) or any(ord(c) < 32 or ord(c) == 127 for c in self.subject):
            raise ValueError("Invalid email proposal headers")
        return self


class WarningActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["warning"] = "warning"
    source: WarningSource


class BillingActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["billing"] = "billing"
    provider: Literal["chatgpt", "api"]
    reason: Literal["connected", "not_connected", "quota", "model_unavailable"]


class PythonActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["python"] = "python"
    id: Identifier
    channel: Literal["code", "output", "image", "status"]
    data: str = Field(max_length=2048)

    @model_validator(mode="after")
    def bounded_fragment(self) -> "PythonActivity":
        if self.channel in {"code", "output"} and len(self.data) > 256:
            raise ValueError("Python text fragment exceeds limit")
        if self.channel == "image" and not re.fullmatch(
            r"[A-Za-z0-9+/]*={0,2}", self.data
        ):
            raise ValueError("Expected base64 image fragment")
        if self.channel == "status" and self.data not in {
            "running",
            "completed",
            "failed",
        }:
            raise ValueError("Invalid execution status")
        return self


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
    PhaseActivity
    | PlanActivity
    | QuestionActivity
    | QuestionResolved
    | WarningActivity
    | BillingActivity
    | PythonActivity
    | EmailDraftActivity,
    Field(discriminator="type"),
]
activity_adapter: TypeAdapter[Activity] = TypeAdapter(Activity)
