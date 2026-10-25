"""Bounded wire contracts; model arguments cannot carry authority."""

from typing import Annotated, Literal

from galadril_pipeline.capabilities import CapabilityId
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from galadril_scribe.teams import TeamDefinition

ReasoningEffort = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max"
]
ModelAlias = Annotated[
    str,
    Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    ),
]


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["image", "audio", "document", "text"]
    url: str = Field(default="", max_length=8192)
    media_type: str | None = Field(default=None, max_length=128)
    trusted_storage: bool = False
    text: str = Field(default="", max_length=65536)

    @model_validator(mode="after")
    def valid_content(self) -> "Attachment":
        if self.kind == "text":
            if not self.text or self.url:
                raise ValueError("Invalid text document")
        elif not self.url or self.text:
            raise ValueError("Invalid media reference")
        return self


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["user", "assistant"]
    content: str = Field(max_length=65536)
    attachments: list[Attachment] = Field(default_factory=list, max_length=8)


class GraphContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["entity", "event", "relation"]
    id: str = Field(min_length=1, max_length=256)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prompt: str = Field(max_length=65536)
    capability: SecretStr = Field(min_length=64, max_length=64)
    model_alias: ModelAlias | None = None
    openai_owner_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reasoning_effort: ReasoningEffort | None = None
    history: list[HistoryMessage] = Field(default_factory=list, max_length=64)
    attachments: list[Attachment] = Field(default_factory=list, max_length=8)
    graph_context: GraphContext | None = None
    web_query: str | None = Field(default=None, min_length=1, max_length=512)
    web_search: bool = False
    library_enabled: bool = False
    tool_ids: tuple[CapabilityId, ...] = Field(default=(), max_length=32)
    team_id: CapabilityId | None = None
    team: TeamDefinition | None = None
    user_instructions: str = Field(default="", max_length=2000)
    project_instructions: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def bounded_context(self) -> "RunRequest":
        """Reject oversized context before provider tokenization allocates it."""
        for value in (self.user_instructions, self.project_instructions):
            if any(
                ord(c) < 32 and c not in "\r\n\t" or ord(c) == 127
                for c in value
            ):
                raise ValueError("Invalid Pengolo instructions")
        if len(set(self.tool_ids)) != len(self.tool_ids):
            raise ValueError("Extension IDs must be unique")
        if self.team is not None and (
            self.team_id is not None
            or not self.team.id.startswith("user.team.")
            or any(
                agent.implementation_id is not None
                for agent in self.team.agents
            )
        ):
            raise ValueError(
                "Delegated teams cannot select trusted implementations"
            )
        if (
            len(self.prompt.encode())
            + sum(len(file.text.encode()) for file in self.attachments)
            + sum(
                len(message.content.encode())
                + sum(len(file.text.encode()) for file in message.attachments)
                for message in self.history
            )
            > 65536
        ):
            raise ValueError("Context exceeds 65536 bytes")
        if not self.prompt.strip() and not self.attachments:
            raise ValueError("A prompt or attachment is required")
        return self


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["content", "tool", "activity", "completed", "failed"]
    content: str = ""


class GenerationRequest(RunRequest):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")


class LibraryFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str


class LibraryDocument(LibraryFile):
    attachment: Attachment


class LibraryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    files: list[LibraryFile] = Field(max_length=8)
    after: str | None


class DocumentGraphReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entityId: str = Field(min_length=1, max_length=256)
    eventId: str = Field(min_length=1, max_length=256)
    stateType: str | None = Field(default=None, max_length=256)


class DocumentHit(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    sourceId: str
    sourceChecksum: str
    unitId: str
    filename: str
    text: str = Field(max_length=16384)
    page: int | None
    similarity: float | None
    relevance: float | None
    graphScore: float | None = Field(default=None, ge=0, le=1)
    eskgRefs: tuple[DocumentGraphReference, ...] = Field(
        default=(), max_length=16
    )


class DocumentPage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    hits: tuple[DocumentHit, ...] = Field(max_length=20)
    warnings: tuple[str, ...] = Field(max_length=16)
    engine: str | None = Field(default=None, max_length=64)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["openai", "vllm"] = "vllm"
    model: str = Field(min_length=1, max_length=256)
    base_url: str | None = None
    api_key: SecretStr | None = Field(default=None, min_length=1)
    reasoning_efforts: tuple[ReasoningEffort, ...] = ()
    default_reasoning_effort: ReasoningEffort | None = None

    @model_validator(mode="after")
    def provider_configuration(self) -> "ModelConfig":
        """Keep official credentials off compatible servers and fail closed on effort."""
        from urllib.parse import urlsplit

        if self.provider == "openai":
            if self.base_url is not None:
                raise ValueError(
                    "Official OpenAI does not accept a custom endpoint"
                )
        elif self.base_url is None:
            raise ValueError("vLLM requires an explicit endpoint")
        else:
            url = urlsplit(self.base_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
            ):
                raise ValueError("Invalid vLLM endpoint")
        if len(set(self.reasoning_efforts)) != len(self.reasoning_efforts):
            raise ValueError("Reasoning efforts must be unique")
        if (
            self.default_reasoning_effort is not None
            and self.default_reasoning_effort not in self.reasoning_efforts
        ):
            raise ValueError("Default reasoning effort is not supported")
        return self


class AvailableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    alias: ModelAlias
    provider: Literal["openai", "vllm"]
    model: str
    reasoning_efforts: tuple[ReasoningEffort, ...]
    default_reasoning_effort: ReasoningEffort | None


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    default_model: ModelAlias | None
    models: tuple[AvailableModel, ...]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCRIBE_",
        extra="forbid",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )
    proxy_mode: Literal["sidecar", "ambient"] = "sidecar"
    host: str = "127.0.0.1"
    port: int = Field(default=8091, ge=1, le=65535)
    gateway_tools_url: str
    models: dict[ModelAlias, ModelConfig] = Field(
        default_factory=dict, max_length=64
    )
    default_model: ModelAlias | None = None
    openai_api_key: SecretStr | None = Field(default=None, min_length=1)
    chatgpt_enabled: bool = False
    chatgpt_credentials_path: str = ""
    chatgpt_encryption_key: SecretStr | None = None
    web_search_provider: Literal["gateway", "openai"] = "gateway"
    python_sandbox_socket: str = ""
    document_model_cache: str = ""
    document_eru_tier: Literal["0.6b", "4b"] = "0.6b"
    document_reranker: Literal["bge", "qwen"] = "bge"
    document_extraction_model: ModelAlias | None = None
    document_remote_relations: bool = True
    document_relation_model: str = Field(
        default="gpt-5.6-terra", min_length=1, max_length=128
    )
    mcp_urls: list[str] = Field(default_factory=list, max_length=8)
    max_concurrent_runs: int = Field(default=1, ge=1, le=32)
    max_output_tokens: int = Field(default=4096, ge=1, le=8192)
    max_tool_calls: int = Field(default=32, ge=1, le=32)
    max_model_requests: int = Field(default=40, ge=1, le=64)
    run_timeout_seconds: int = Field(default=600, ge=1, le=900)

    @model_validator(mode="after")
    def configured_model(self) -> "Settings":
        """Require an explicit model to avoid unintended provider selection."""
        import base64
        import binascii
        from ipaddress import ip_address
        from pathlib import Path

        if self.chatgpt_enabled:
            if (
                self.chatgpt_encryption_key is None
                or not Path(self.chatgpt_credentials_path).is_absolute()
            ):
                raise ValueError(
                    "ChatGPT requires protected credential storage"
                )
            try:
                key = base64.b64decode(
                    self.chatgpt_encryption_key.get_secret_value(),
                    validate=True,
                )
            except (ValueError, binascii.Error):
                raise ValueError("Invalid ChatGPT encryption key") from None
            if len(key) != 32:
                raise ValueError("ChatGPT encryption key must contain 32 bytes")

        if self.python_sandbox_socket and (
            not self.python_sandbox_socket.startswith("/")
            or "\x00" in self.python_sandbox_socket
        ):
            raise ValueError(
                "Python sandbox requires an absolute Unix socket path"
            )

        if (
            not ip_address(self.host).is_loopback
            and self.proxy_mode != "ambient"
        ):
            raise ValueError("Scribe must listen on loopback behind Envoy")
        if any(
            config.provider == "openai"
            and config.api_key is None
            and self.openai_api_key is None
            and not self.chatgpt_enabled
            for config in self.models.values()
        ):
            raise ValueError("OpenAI requires an API key")
        return self
