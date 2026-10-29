"""Bounded email contracts keep connection authority outside pipeline data."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

DEFAULT_SIGNATURE = ""
SIGNATURE_FIELDS = frozenset({"ref", "subject", "sender", "recipient", "date"})
SIGNATURE_FIELD = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

type Right = Literal[
    "view",
    "read",
    "use",
    "edit",
    "delete",
    "reveal",
    "share",
    "send",
    "approve",
    "automate",
]
RIGHTS: tuple[Right, ...] = (
    "view",
    "read",
    "use",
    "edit",
    "delete",
    "reveal",
    "share",
    "send",
    "approve",
    "automate",
)


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )


class Principal(Contract):
    tenant: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    user: str = Field(
        min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_@.:-]+$"
    )


def validate_address(value: str) -> str:
    if not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
        value,
    ):
        raise ValueError("Invalid email address")
    return value


type SmtpPort = Literal[25, 80, 443, 465, 587, 2525, 8025, 8465]


class ConnectionInput(Contract):
    name: str = Field(min_length=1, max_length=128)
    sender_name: str = Field(default="", max_length=128)
    address: str = Field(max_length=320)
    imap_host: str = Field(
        default="",
        max_length=253,
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9])?$",
    )
    imap_port: Literal[993] = 993
    smtp_host: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$",
    )
    smtp_port: SmtpPort = 465
    folders: tuple[str, ...] = Field(
        default=("INBOX",), min_length=1, max_length=16
    )
    enabled: bool = True
    poll_seconds: int = Field(default=120, ge=60, le=86400)
    ingest: bool = False
    initial_sync: Literal["new", "all"] = "new"

    _address = field_validator("address")(validate_address)

    @model_validator(mode="after")
    def reception(self) -> ConnectionInput:
        if not self.imap_host and self.ingest:
            raise ValueError(
                "Receiving must be configured before ingestion can be enabled"
            )
        return self

    @field_validator("sender_name")
    @classmethod
    def sender_name_safe(cls, value: str) -> str:
        if any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("Invalid sender display name")
        return value.strip()

    @field_validator("folders")
    @classmethod
    def folders_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            not re.fullmatch(r"[A-Za-z0-9_ /.-]{1,128}", item) for item in value
        ):
            raise ValueError("Invalid folders")
        return value


class Credentials(Contract):
    username: str = Field(min_length=1, max_length=320, repr=False)
    password: SecretStr = Field(min_length=1, max_length=8192)
    auth: Literal["password", "google", "microsoft"] = "password"
    refresh_token: SecretStr | None = None
    expires_at: int = 0

    @field_validator("username")
    @classmethod
    def no_controls(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid username")
        return value

    @field_validator("password")
    @classmethod
    def safe_password(cls, value: SecretStr) -> SecretStr:
        if any(char in value.get_secret_value() for char in "\x00\r\n"):
            raise ValueError("Invalid credential encoding")
        return value


class PasswordReplacement(Contract):
    password: SecretStr = Field(min_length=1, max_length=8192)

    @field_validator("password")
    @classmethod
    def safe_password(cls, value: SecretStr) -> SecretStr:
        return Credentials.safe_password(value)


class Attachment(Contract):
    name: str = Field(max_length=255)
    content_type: str = Field(max_length=128)
    content: str = Field(max_length=16 * 1024 * 1024)


class MailMessage(Contract):
    reference: str = Field(default="", pattern=r"^(?:GDR-[A-F0-9]{32})?$")
    thread_reference: str = Field(default="", max_length=1050)
    thread_number: int = Field(default=0, ge=0)
    message_id: str = Field(max_length=998)
    thread_id: str = Field(default="", max_length=998)
    sender: str = Field(max_length=320)
    recipients: tuple[str, ...] = Field(max_length=100)
    subject: str = Field(max_length=998)
    date: str = Field(max_length=128)
    body: str = Field(max_length=1024 * 1024)
    references: str = Field(max_length=8192)
    reply_allowed: bool
    attachments: tuple[Attachment, ...] = Field(default=(), max_length=32)


class DraftInput(Contract):
    track_opens: bool = False
    reference: str = Field(default="", pattern=r"^(?:GDR-[A-F0-9]{32})?$")
    thread_reference: str = Field(default="", max_length=1050)
    thread_number: int = Field(default=0, ge=0)
    thread_id: str = Field(default="", max_length=998)
    to: tuple[str, ...] = Field(min_length=1, max_length=20)
    cc: tuple[str, ...] = Field(default=(), max_length=20)
    bcc: tuple[str, ...] = Field(default=(), max_length=20)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=262144)
    reply_to_message_id: str = Field(default="", max_length=998)
    references: str = Field(default="", max_length=8192)
    attachments: tuple[Attachment, ...] = Field(default=(), max_length=16)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def recipients_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_address(item) for item in value)

    @field_validator(
        "subject",
        "reply_to_message_id",
        "references",
        "thread_reference",
        "thread_id",
    )
    @classmethod
    def safe_header(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid header")
        return value


class ThreadSummary(Contract):
    root: str = Field(max_length=998)
    message_id: str = Field(max_length=998)
    parent: str = Field(default="", max_length=998)
    subject: str = Field(max_length=998)
    date: str = Field(default="", max_length=128)
    source_key: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=128)
    analysis: str = Field(default="", max_length=4096)


class ReplyComposition(Contract):
    generation: Literal["template", "scribe"] = "scribe"
    template: str = Field(default="", max_length=8192)
    model_alias: str | None = Field(default=None, min_length=1, max_length=128)
    instructions: str = Field(default="", max_length=8192)

    @model_validator(mode="after")
    def known_template_fields(self) -> ReplyComposition:
        """Templates substitute plain canonical fields without evaluating expressions."""
        if self.generation == "template":
            if not self.template.strip():
                raise ValueError("Reply template required")
            if any(
                name not in {"sender", "recipient", "subject", "body", "date"}
                for name in SIGNATURE_FIELD.findall(self.template)
            ):
                raise ValueError("Unknown reply template field")
            remainder = SIGNATURE_FIELD.sub("", self.template)
            if "{{" in remainder or "}}" in remainder:
                raise ValueError("Invalid reply template placeholder")
        return self

    def render(self, message: MailMessage) -> str:
        """Only the canonical source message supplies values; rendering cannot add recipients."""
        fields = {
            "sender": message.sender,
            "recipient": ", ".join(message.recipients),
            "subject": message.subject,
            "body": message.body[:8192],
            "date": message.date,
        }
        body = SIGNATURE_FIELD.sub(
            lambda match: fields[match.group(1)], self.template
        )
        if not body.strip() or len(body) > 16384:
            raise ValueError("Reply template result exceeds bounds")
        return body


class PipelineBinding(Contract):
    pipeline_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{20,128}$")


class PipelineMailPolicy(Contract):
    sender_account_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    mode: Literal["draft", "approval", "automatic"]


class DeployedMailPipeline(PipelineBinding):
    policy: PipelineMailPolicy


class ResponseProfile(Contract):
    pipeline: PipelineBinding | None = None
    track_opens: bool = False
    model: str = Field(min_length=1, max_length=128)
    fallbacks: tuple[str, ...] = Field(default=(), max_length=4)
    prompt: str = Field(
        default="Write a helpful, accurate reply. Do not invent facts or commitments.",
        min_length=1,
        max_length=8192,
    )
    skill_ids: tuple[str, ...] = Field(
        default=("utils.skill.email",), max_length=8
    )
    language: str = Field(default="English", max_length=64)
    tone: str = Field(default="Professional", max_length=128)
    signature: str = Field(default=DEFAULT_SIGNATURE, max_length=8192)
    mode: Literal["draft", "approval", "automatic"] = "draft"
    allowed_senders: tuple[str, ...] = Field(default=(), max_length=100)
    daily_limit: int = Field(default=10, ge=1, le=100)
    sender_daily_limit: int | None = Field(default=None, ge=1, le=100)
    one_reply_per_thread: bool = False

    @field_validator("signature")
    @classmethod
    def known_signature_fields(cls, value: str) -> str:
        if any(
            name not in SIGNATURE_FIELDS
            for name in SIGNATURE_FIELD.findall(value)
        ):
            raise ValueError("Unknown signature field")
        return value

    _senders = field_validator("allowed_senders")(
        lambda value: tuple(validate_address(item) for item in value)
    )
