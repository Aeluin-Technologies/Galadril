"""Bounded wire contracts; model arguments cannot carry authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["image", "audio", "document"]
    url: str = Field(min_length=1, max_length=8192)


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["user", "assistant"]
    content: str = Field(max_length=65536)
    attachments: list[Attachment] = Field(default_factory=list, max_length=8)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prompt: str = Field(max_length=65536)
    capability: SecretStr = Field(min_length=64, max_length=64)
    model_alias: str | None = Field(default=None, max_length=128)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=64)
    attachments: list[Attachment] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bounded_context(self) -> "RunRequest":
        """Reject oversized context before provider tokenization allocates it."""
        if (
            len(self.prompt.encode())
            + sum(len(message.content.encode()) for message in self.history)
            > 65536
        ):
            raise ValueError("Context exceeds 65536 bytes")
        if not self.prompt.strip() and not self.attachments:
            raise ValueError("A prompt or attachment is required")
        return self


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["content", "completed", "failed"]
    content: str = ""


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = Field(min_length=1)
    base_url: str
    api_key: SecretStr = SecretStr("local")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCRIBE_", extra="forbid")
    proxy_mode: Literal["sidecar", "ambient"] = "sidecar"
    host: str = "127.0.0.1"
    port: int = Field(default=8091, ge=1, le=65535)
    gateway_tools_url: str
    models: dict[str, ModelConfig]
    default_model: str
    mcp_urls: list[str] = Field(default_factory=list, max_length=8)
    max_concurrent_runs: int = Field(default=1, ge=1, le=32)
    max_output_tokens: int = Field(default=1024, ge=1, le=8192)
    max_tool_calls: int = Field(default=8, ge=1, le=32)
    run_timeout_seconds: int = Field(default=600, ge=1, le=900)

    @model_validator(mode="after")
    def configured_model(self) -> "Settings":
        """Require an explicit model to avoid unintended provider selection."""
        from ipaddress import ip_address

        if (
            not ip_address(self.host).is_loopback
            and self.proxy_mode != "ambient"
        ):
            raise ValueError("Scribe must listen on loopback behind Envoy")
        if self.default_model not in self.models:
            raise ValueError("Default model is not configured")
        return self
