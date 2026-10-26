"""Bounded template packages contain data and never executable bindings."""

from __future__ import annotations

import base64
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class TemplateAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=192)
    data: str = Field(max_length=2 * 1024 * 1024)
    encoding: Literal["utf8", "base64"] = "utf8"

    def content(self) -> bytes:
        return (
            self.data.encode()
            if self.encoding == "utf8"
            else base64.b64decode(self.data, validate=True)
        )

    @model_validator(mode="after")
    def bounded(self) -> TemplateAsset:
        if (
            any(
                part in {"", ".", ".."} or part.startswith(".")
                for part in self.path.split("/")
            )
            or not all(
                char.isascii() and (char.isalnum() or char in "_/.-")
                for char in self.path
            )
            or len(self.content()) > 2 * 1024 * 1024
        ):
            raise ValueError("Invalid template asset")
        return self


class TemplateVariable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["text", "integer", "boolean", "choice"]
    required: bool = True
    choices: tuple[str, ...] = Field(default=(), max_length=64)


class TemplateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str | None = Field(default=None, pattern="^[0-9a-f]{32}$")
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4096)
    scope: Literal["personal", "project", "tenant", "plugin"] = "personal"
    project: str | None = Field(default=None, pattern="^[0-9a-f]{32}$")
    plugin: str | None = Field(default=None, max_length=64)
    entrypoint: str = Field(max_length=192)
    assets: tuple[TemplateAsset, ...] = Field(min_length=1, max_length=64)
    variables: dict[str, TemplateVariable] = Field(
        default_factory=dict, max_length=64
    )

    @model_validator(mode="after")
    def bounded(self) -> TemplateDraft:
        if sum(len(asset.content()) for asset in self.assets) > 2 * 1024 * 1024:
            raise ValueError("Template project exceeds limit")
        if len({asset.path for asset in self.assets}) != len(self.assets):
            raise ValueError("Duplicate template asset")
        if self.entrypoint not in {asset.path for asset in self.assets}:
            raise ValueError("Missing template entrypoint")
        return self

    def wire(self) -> dict[str, JsonValue]:
        result = cast(dict[str, JsonValue], self.model_dump(mode="json"))
        result["assets"] = [
            {"path": asset.path, "data": list(asset.content())}
            for asset in self.assets
        ]
        return result
