"""Bounded classification contracts shared by native Vision and Scribe routing."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ModelAlias = Annotated[
    str,
    Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    ),
]
BranchLabel = Annotated[str, Field(min_length=1, max_length=128)]
ReasoningEffort = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max"
]


class BranchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    branch: BranchLabel


class RoutingRequest(BaseModel):
    """Carries untrusted content and model aliases, never tools or provider settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(max_length=65536)
    labels: tuple[BranchLabel, ...] = Field(min_length=1, max_length=32)
    instructions: str = Field(min_length=1, max_length=8192)
    model_alias: ModelAlias | None = None
    fallback_models: tuple[ModelAlias, ...] | None = Field(
        default=None, max_length=8
    )
    reasoning_effort: ReasoningEffort | None = None
    timeout_seconds: float = Field(default=60, gt=0, le=180)

    @model_validator(mode="after")
    def unique_choices(self) -> RoutingRequest:
        if len(set(self.labels)) != len(self.labels) or (
            self.fallback_models is not None
            and len(set(self.fallback_models)) != len(self.fallback_models)
        ):
            raise ValueError(
                "Routing labels and fallback aliases must be unique"
            )
        return self


class RoutingDecision(BranchDecision):
    model_alias: ModelAlias
    fallback: bool
