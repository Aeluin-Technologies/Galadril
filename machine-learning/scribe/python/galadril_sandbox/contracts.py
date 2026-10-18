"""Sandbox policy accepts package names, never indexes, URLs or install flags."""

import base64
import binascii
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Library = Literal[
    "numpy", "matplotlib", "pandas", "scipy", "sympy", "pillow", "scikit-learn"
]


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str = Field(min_length=1, max_length=8192)
    libraries: tuple[Library, ...] = Field(default=(), max_length=7)

    @model_validator(mode="after")
    def bounded(self) -> "ExecutionRequest":
        if (
            not self.code.strip()
            or len(self.code.encode()) > 8192
            or len(set(self.libraries)) != len(self.libraries)
        ):
            raise ValueError("Invalid code or library selection")
        return self


class InstalledLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Library
    version: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9.+-]+$"
    )


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["completed", "failed"]
    failure: (
        Literal[
            "input",
            "catalog",
            "environment",
            "installation",
            "execution",
            "artifact",
        ]
        | None
    ) = None
    text: str = Field(default="", max_length=65536)
    image: str | None = Field(default=None, max_length=174764)
    libraries: tuple[InstalledLibrary, ...] = Field(default=(), max_length=7)

    @model_validator(mode="after")
    def bounded(self) -> "ExecutionResult":
        if len(self.text.encode()) > 65536:
            raise ValueError("Output exceeds limit")
        if self.image is not None:
            try:
                data = base64.b64decode(self.image, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("Invalid PNG") from None
            if (
                len(data) > 131072
                or len(data) < 33
                or not data.startswith(b"\x89PNG\r\n\x1a\n")
                or data[12:16] != b"IHDR"
                or not 0 < int.from_bytes(data[16:20]) <= 2000
                or not 0 < int.from_bytes(data[20:24]) <= 2000
            ):
                raise ValueError("Invalid bounded PNG")
        return self
