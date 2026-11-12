"""Contracts for explicit signing requests and unverified provider artifacts."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PDF_BYTES = 16 * 1024 * 1024


class SignatureKind(StrEnum):
    HANDWRITTEN = "handwritten"
    CRYPTOGRAPHIC = "cryptographic"
    QUALIFIED_REQUESTED = "qualified_requested"


class ProviderStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ProviderError(RuntimeError):
    """A mutation with an ambiguous outcome must be reconciled before retrying."""

    __slots__ = ("reconcile_before_retry",)

    def __init__(
        self, message: str, *, reconcile_before_retry: bool = False
    ) -> None:
        super().__init__(message)
        self.reconcile_before_retry = reconcile_before_retry


class Placement(BaseModel):
    """Percentages of the unrotated page, measured from its top left."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,55}$")]
    page: Annotated[int, Field(ge=1, le=10000)]
    x: Annotated[float, Field(ge=0, lt=100, allow_inf_nan=False)]
    y: Annotated[float, Field(ge=0, lt=100, allow_inf_nan=False)]
    width: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)]
    height: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)]

    @model_validator(mode="after")
    def inside_page(self) -> "Placement":
        if self.x + self.width > 100 or self.y + self.height > 100:
            raise ValueError("Signature field is outside the page")
        return self


class Signer(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    name: str = Field(min_length=1, max_length=120, repr=False)
    email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+$", repr=False
    )
    placement: Placement


class EnvelopeDraft(BaseModel):
    """The calling service owns authorization and the immutable source revision."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    request_id: UUID
    title: str = Field(min_length=1, max_length=120, repr=False)
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: SignatureKind
    signers: tuple[Signer, ...] = Field(min_length=1, max_length=16, repr=False)

    @model_validator(mode="after")
    def unique_assignments(self) -> "EnvelopeDraft":
        if len({signer.email.casefold() for signer in self.signers}) != len(
            self.signers
        ):
            raise ValueError("Duplicate signer")
        if len({signer.placement.name for signer in self.signers}) != len(
            self.signers
        ):
            raise ValueError("Duplicate signature field")
        return self


@dataclass(frozen=True, slots=True)
class ProviderArtifact:
    """Transport evidence alone does not validate a PDF signature or its tier."""

    pdf: bytes = field(repr=False)
    sha256: str


class SignatureProvider(Protocol):
    async def create_draft(
        self, draft: EnvelopeDraft, original: bytes
    ) -> str: ...

    async def status(
        self, ref: str, draft: EnvelopeDraft
    ) -> ProviderStatus: ...

    async def distribute(self, ref: str, draft: EnvelopeDraft) -> None: ...

    async def cancel(self, ref: str, draft: EnvelopeDraft) -> None: ...

    async def download_completed(
        self, ref: str, draft: EnvelopeDraft
    ) -> ProviderArtifact: ...
