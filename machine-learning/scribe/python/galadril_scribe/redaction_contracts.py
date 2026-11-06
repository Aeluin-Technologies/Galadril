"""Redaction rules describe matches; they cannot carry code or authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_WIRE_BYTES = 24 * 1024 * 1024
MAX_TEXT_CHARS = 1024 * 1024
MAX_MATCHES = 4096


class RedactionRule(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    kind: Literal["email", "phone", "literal", "regex"]
    pattern: SecretStr | None = None
    replacement: str = Field(default="[REDACTED]", min_length=1, max_length=64)
    case_sensitive: bool = False
    replacement_mode: Literal["literal", "initials"] = "literal"

    @model_validator(mode="after")
    def bounded_pattern(self) -> "RedactionRule":
        pattern = (
            self.pattern.get_secret_value()
            if self.pattern is not None
            else None
        )
        if self.kind in {"literal", "regex"}:
            if pattern is None or not pattern or len(pattern) > 512:
                raise ValueError("Invalid redaction pattern")
        elif pattern is not None:
            raise ValueError("Built-in rules cannot override their pattern")
        if self.replacement_mode == "initials" and (
            self.kind not in {"literal", "regex"}
            or self.replacement != "[REDACTED]"
        ):
            raise ValueError(
                "Initials require a name match without a literal replacement"
            )
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in self.replacement):
            raise ValueError("Invalid public replacement")
        return self


class RedactionInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    mime: Literal["application/pdf", "text/plain"]
    content_base64: SecretStr = Field(max_length=MAX_WIRE_BYTES)
    rules: tuple[RedactionRule, ...] = Field(min_length=1, max_length=16)


class RedactionResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    mime: Literal["application/pdf", "text/plain"]
    content_base64: str = Field(max_length=MAX_WIRE_BYTES)
    match_count: int = Field(ge=0, le=MAX_MATCHES)
    pages: int = Field(ge=0, le=128)
    requires_review: Literal[True] = True
    layout_preserved: bool = True
    coverage: Literal["provided_rules_only"] = "provided_rules_only"
    warnings: tuple[Literal["annotations_removed", "no_matches"], ...] = ()
