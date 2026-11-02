"""Deterministic email observations keep participant identity separate from inference."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import structlog
from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_plugins import PluginScope
from galadril_plugins.utils.models import Contract, validate_address
from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, field_validator

_OBJECT = TypeAdapter(dict[str, JsonValue])


class EmailMetadata(Contract):
    model_config = ConfigDict(extra="ignore", frozen=True)
    source_kind: Literal["email"]
    account_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    email_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    message_id: str = Field(min_length=1, max_length=998)
    thread_id: str = Field(default="", max_length=998)
    sender: str = Field(max_length=320)
    recipients: tuple[str, ...] = Field(max_length=100)
    subject: str = Field(default="", max_length=998)
    date: str = Field(default="", max_length=128)

    _sender = field_validator("sender")(validate_address)

    @field_validator("recipients")
    @classmethod
    def addresses(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_address(address) for address in value)


@dataclass(frozen=True, slots=True)
class EmailParticipants:
    scope: PluginScope

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        """Use tenant-qualified mailbox identities so unrelated tenants cannot merge people."""
        del step
        if command.tenant_id != self.scope.tenant_id:
            raise ValueError("Email tenant mismatch")
        record = _OBJECT.validate_python(command.payload.get("record"))
        raw = _OBJECT.validate_python(record.get("raw_payload"))
        metadata = EmailMetadata.model_validate(raw.get("metadata"))
        participants: dict[str, list[str]] = {
            metadata.sender.casefold(): ["sender"]
        }
        for recipient in metadata.recipients:
            roles = participants.setdefault(recipient.casefold(), [])
            if "recipient" not in roles:
                roles.append("recipient")
        data: list[JsonValue] = []
        for address, roles in participants.items():
            identifier = hashlib.sha256(
                (self.scope.tenant_id + "\0" + address).encode()
            ).hexdigest()
            data.append(
                {
                    "resolved_entity_id": "email_party_" + identifier,
                    "label": address,
                    "entity_type": "email_party",
                    "state_type": "email_participant",
                    "email": address,
                    "roles": roles,
                    "message_id": metadata.message_id,
                    "thread_id": metadata.thread_id,
                    "subject": metadata.subject,
                    "account_id": metadata.account_id,
                    "email_id": metadata.email_id,
                }
            )
        try:
            timestamp = parsedate_to_datetime(metadata.date)
            if timestamp.tzinfo is not None:
                record["timestamp"] = timestamp.astimezone(UTC).isoformat()
        except (ValueError, TypeError, OverflowError):
            # A malformed Date header must not erase Intake's authenticated timestamp.
            structlog.get_logger(__name__).warning(
                "email.pipeline.invalid_date"
            )
        record["event_type"] = "Observation"
        return {**command.payload, "record": record, "data": data}


def email_participants(
    scope: PluginScope, step: PipelineStep
) -> EmailParticipants:
    """Participant extraction has no network, credential or model authority."""
    del step
    return EmailParticipants(scope)


type MailOperation = Literal["reply", "compose", "context", "decide"]


def _reply_references(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Carries opaque upstream references; only the worker may authorize their effects."""
    references: dict[str, JsonValue] = {}
    for field, source, target in (
        ("email_reply_decision", "decision_id", "decision_id"),
        ("email_reply", "id", "draft_id"),
    ):
        value = payload.get(field)
        if isinstance(value, dict) and isinstance(value.get(source), str):
            references[target] = value[source]
    return references


def _mail_request(
    scope: PluginScope,
    command: PipelineCommand,
    step: PipelineStep,
    operation: MailOperation,
) -> dict[str, JsonValue]:
    """Separates trusted Registry scope from untrusted event and model payloads."""
    if command.tenant_id != scope.tenant_id:
        raise ValueError("Email tenant mismatch")
    record = _OBJECT.validate_python(command.payload.get("record"))
    raw = _OBJECT.validate_python(record.get("raw_payload"))
    metadata = EmailMetadata.model_validate(raw.get("metadata"))
    request: dict[str, JsonValue] = {
        "tenant": scope.tenant_id,
        "pipeline_id": scope.pipeline_id,
        "revision_id": scope.revision_id,
        "account_id": metadata.account_id,
        "email_id": metadata.email_id,
    }
    if operation == "decide":
        from galadril_plugins.utils.models import ReplyDecisionOptions

        decision_options = ReplyDecisionOptions.model_validate(
            step.params.model_extra or {}
        )
        request.update(decision_options.model_dump(mode="json"))
    elif operation == "context":
        from galadril_plugins.utils.models import EmailContextOptions

        context_options = EmailContextOptions.model_validate(
            step.params.model_extra or {}
        )
        request.update(context_options.model_dump(mode="json"))
    elif operation == "compose":
        from galadril_plugins.utils.models import ReplyComposition

        composition = ReplyComposition.model_validate(
            step.params.model_extra or {}
        )
        request["composition"] = composition.model_dump(mode="json")
    else:
        request.update(_reply_references(command.payload))
    return request


def _mail_endpoint(endpoint: str) -> str:
    """Rejects credentials and request-target overrides in an operator service origin."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Email service deployment unavailable")
    return endpoint


async def _mail_service() -> tuple[str, str]:
    """Only deployment configuration can select an endpoint or a private credential."""
    endpoint = _mail_endpoint(
        os.environ.get("UTILS_SERVICE_URL", "").rstrip("/")
    )
    key_file = os.environ.get("UTILS_SERVICE_KEY_FILE", "")
    if not key_file:
        raise ValueError("Email service credential unavailable")
    key = (await asyncio.to_thread(Path(key_file).read_text)).strip()
    if len(key) < 32 or any(char in key for char in "\r\n"):
        raise ValueError("Email service credential unavailable")
    return endpoint, key


@dataclass(frozen=True, slots=True)
class EmailReply:
    scope: PluginScope
    operation: MailOperation = "reply"

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        """Only operator settings select the mail service; payloads never carry credentials."""
        request = _mail_request(self.scope, command, step, self.operation)
        endpoint, key = await _mail_service()
        async with httpx.AsyncClient(
            timeout=150, follow_redirects=False, trust_env=False
        ) as http:
            async with http.stream(
                "POST",
                endpoint + "/pipeline/" + self.operation,
                headers={"x-utils-key": key},
                json=request,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    limit = {
                        "reply": 4096,
                        "compose": 65536,
                        "context": 262144,
                        "decide": 262144,
                    }[self.operation]
                    if len(body) + len(chunk) > limit:
                        raise ValueError(
                            "Email pipeline response exceeds limit"
                        )
                    body.extend(chunk)
                result = _OBJECT.validate_json(body)
        if self.operation == "context":
            return {**command.payload, "email_context": result}
        if self.operation == "decide":
            return {
                **command.payload,
                "email_reply_decision": result,
                "email_reply": result,
            }
        return {**command.payload, "email_reply": result}


def email_reply(scope: PluginScope, step: PipelineStep) -> EmailReply:
    """The factory receives immutable Registry scope, not caller-selected authority."""
    del step
    return EmailReply(scope)


def compose_email_reply(scope: PluginScope, step: PipelineStep) -> EmailReply:
    """Generation is a separate operation; only the output action can request sending."""
    del step
    return EmailReply(scope, operation="compose")


def email_context(scope: PluginScope, step: PipelineStep) -> EmailReply:
    """Context extraction has no draft, approval or sending operation."""
    del step
    return EmailReply(scope, operation="context")


def decide_email_reply(scope: PluginScope, step: PipelineStep) -> EmailReply:
    """A decision can suppress sending but cannot approve its own draft."""
    del step
    return EmailReply(scope, operation="decide")
