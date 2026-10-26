"""Deterministic email observations keep participant identity separate from inference."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Literal

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


