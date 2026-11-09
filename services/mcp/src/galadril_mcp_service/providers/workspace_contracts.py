"""Public MCP schemas describe the existing Gateway and compiler wire contracts."""

from typing import Annotated, Literal
from uuid import UUID

from galadril_plugins.document_templates import TemplateDraft
from galadril_plugins.utils.models import DraftInput
from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )


class MailRead(RequestModel):
    operation: Literal[
        "accounts",
        "contacts",
        "messages",
        "message",
        "thread",
        "attachment",
        "drafts",
        "draft",
        "proposal_status",
    ]
    account: Identifier | None = None
    message: Identifier | None = None
    draft: Identifier | None = None
    query: str = Field(default="", max_length=512)
    before: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0, le=1024 * 1024)
    attachment: int | None = Field(default=None, ge=0, le=31)


class SaveDraft(RequestModel):
    operation: Literal["save_draft"] = "save_draft"
    account: Identifier
    draft: Identifier | None = None
    revision: int | None = Field(default=None, ge=1)
    content: DraftInput


class TemplateList(RequestModel):
    action: Literal["list"]
    after: str = Field(default="", max_length=256)


class TemplateCatalog(RequestModel):
    action: Literal["plugin_catalog"]


class TemplateRead(RequestModel):
    action: Literal["read"]
    template_id: Identifier
    version: Revision
    path: str | None = Field(default=None, max_length=192)
    offset: int = Field(default=0, ge=0, le=2 * 1024 * 1024)


TemplateQuery = Annotated[
    TemplateList | TemplateCatalog | TemplateRead, Field(discriminator="action")
]


class TemplatePut(RequestModel):
    action: Literal["put"]
    template: TemplateDraft


class TemplateMutation(RequestModel):
    action: Literal["publish", "delete"]
    template_id: Identifier
    version: Revision


class TemplateExport(RequestModel):
    action: Literal["export"]
    template_id: Identifier
    version: Revision
    paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    name: str = Field(default="sources.zip", max_length=256)


class DocumentCase(RequestModel):
    action: Literal["document_case"]
    template_id: Identifier
    fields: dict[str, str] | None = Field(default=None, max_length=8)


TemplateWrite = Annotated[
    TemplatePut | TemplateMutation | TemplateExport | DocumentCase,
    Field(discriminator="action"),
]


class Document(RequestModel):
    variables: dict[str, str | int | bool] = Field(
        default_factory=dict, max_length=64
    )
    source: str | None = Field(default=None, max_length=1024 * 1024)


class Compilation(RequestModel):
    template_id: Identifier
    version: Revision
    idempotency_key: UUID
    document: Document


class JobStatus(RequestModel):
    action: Literal["status", "cancel"]
    job_id: Revision


class JobSave(RequestModel):
    action: Literal["save"]
    job_id: Revision
    name: str = Field(default="document.pdf", max_length=256)


class JobBatch(RequestModel):
    action: Literal["save_batch"]
    job_ids: tuple[Revision, ...] = Field(min_length=2, max_length=8)
    name: str = Field(default="documents.zip", max_length=256)


JobRequest = Annotated[
    JobStatus | JobSave | JobBatch, Field(discriminator="action")
]
