"""Business predicates are bounded and cannot select arbitrary payload paths."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    model_validator,
)

_METADATA_FIELDS = {"recipient": "recipients", "account": "account_id"}


class BusinessCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: Literal["sender", "recipient", "subject", "body", "account"]
    operator: Literal["equals", "contains", "starts_with", "not_equals"]
    value: str = Field(min_length=1, max_length=2048)
    _normalized: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def normalize_comparison(self) -> BusinessCondition:
        """Normalize rule values once instead of on each streaming event."""
        if not self.value.strip():
            raise ValueError("Condition value required")
        self._normalized = self.value.casefold()
        return self


class ConditionParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["route", "filter"] = "filter"
    conditions: tuple[BusinessCondition, ...] = Field(
        min_length=1, max_length=16
    )
    match_branch: str = Field(default="match", min_length=1, max_length=128)
    default_branch: str = Field(default="other", min_length=1, max_length=128)

    def matches(self, payload: Mapping[str, JsonValue]) -> bool:
        """Missing fields fail closed, including negative comparisons."""
        record = payload.get("record")
        raw = record.get("raw_payload") if isinstance(record, dict) else None
        metadata = raw.get("metadata") if isinstance(raw, dict) else None
        if not isinstance(raw, dict):
            return False
        for condition in self.conditions:
            if condition.field == "body":
                actual = raw.get("content")
            else:
                key = _METADATA_FIELDS.get(condition.field, condition.field)
                actual = (
                    metadata.get(key) if isinstance(metadata, dict) else None
                )
            values = actual if isinstance(actual, list) else (actual,)
            if not values or any(
                not isinstance(value, str) for value in values
            ):
                return False
            matched = False
            for value in values:
                if not isinstance(value, str):
                    return False
                candidate = value[:65536].casefold()
                expected = condition._normalized
                if condition.operator in {"equals", "not_equals"}:
                    matched = matched or candidate == expected
                elif condition.operator == "contains":
                    matched = matched or expected in candidate
                else:
                    matched = matched or candidate.startswith(expected)
            if condition.operator == "not_equals":
                matched = not matched
            if not matched:
                return False
        return True
