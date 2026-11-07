"""Bounded public progress excludes hidden reasoning and execution authority."""

import re
from typing import Annotated, Literal

from galadril_plugins.utils.models import ReplySource
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)
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
    reply_source: ReplySource | None = None

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


class WebSite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    origin: str = Field(max_length=300)
    result_count: int = Field(ge=1, le=8, strict=True)

    @field_validator("origin")
    @classmethod
    def public_origin(cls, value: str) -> str:
        if not re.fullmatch(
            r"https?://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?[.])+[a-z][a-z0-9-]{1,62}",
            value,
        ) or value.endswith(
            (
                ".local",
                ".localhost",
                ".internal",
                ".invalid",
                ".test",
                ".lan",
                ".home",
                ".localdomain",
                ".example",
            )
        ):
            raise ValueError(
                "Expected a public origin without paths or credentials"
            )
        return value


class WebResearchMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sites: tuple[WebSite, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def bounded(self) -> "WebResearchMetadata":
        if (
            len({site.origin for site in self.sites}) != len(self.sites)
            or sum(site.result_count for site in self.sites) > 8
        ):
            raise ValueError("Invalid search site inventory")
        return self


class WebResearchActivity(WebResearchMetadata):
    sites: tuple[WebSite, ...] = Field(min_length=1, max_length=8)
    type: Literal["web_research"] = "web_research"
    id: Identifier


class WebSearchEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    research: WebResearchMetadata | None = None


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


class UsageActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["usage"] = "usage"
    requests: int = Field(ge=0, le=1000000)
    tool_calls: int = Field(ge=0, le=10000000)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    stop_reason: Literal["budget", "timeout", "error"] | None = None


class ToolActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["tool_status"] = "tool_status"
    id: Identifier
    name: Identifier
    status: Literal["started", "completed", "failed"]


Activity = Annotated[
    PhaseActivity
    | UsageActivity
    | ToolActivity
    | PlanActivity
    | QuestionActivity
    | QuestionResolved
    | WebResearchActivity
    | WarningActivity
    | BillingActivity
    | PythonActivity
    | EmailDraftActivity,
    Field(discriminator="type"),
]
activity_adapter: TypeAdapter[Activity] = TypeAdapter(Activity)
