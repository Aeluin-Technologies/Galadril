"""Runtime validation schemas for state persistence and telemetry tracking."""

from __future__ import annotations
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator

from galadril_pipeline.config import CleanStr
from galadril_pipeline.compiler.resources import NodeStatus


class StepCheckpoint(BaseModel):
    """Immutable checkpoint record ensuring processing idempotence and replay integrity."""

    model_config = ConfigDict(
        slots=True, strict=True, frozen=True, extra="forbid"
    )

    step_name: CleanStr = Field(
        description="Unique identifier of the pipeline step."
    )
    correlation_id: CleanStr = Field(
        description="Distributed trace tracking identifier."
    )
    status: NodeStatus = Field(description="Final runtime resolution status.")
    completed_at: datetime = Field(
        description="UTC timestamp marking execution completion."
    )
    records_processed: int = Field(
        ge=0, description="Total records mutated during execution."
    )
    storage_uri_pointers: list[CleanStr] = Field(
        default_factory=list,
        description="Pointers to immutable artifacts written to storage.",
    )
    payload_checksum: CleanStr = Field(
        description="Cryptographic SHA-256 signature validating state boundary integrity."
    )

    @field_validator("payload_checksum")
    @classmethod
    def validate_checksum_format(cls, value: str) -> str:
        """Validates that the payload checksum is a valid SHA-256 hexadecimal string."""
        if len(value) != 64 or not all(
            c in "0123456789abcdefABCDEF" for c in value
        ):
            raise ValueError(
                "Payload checksum must be a 64-character hexadecimal string."
            )
        return value.lower()


class PipelineRunContext(BaseModel):
    """Global immutable run-scoped token containing execution tracking metadata."""

    model_config = ConfigDict(
        slots=True, strict=True, frozen=True, extra="forbid"
    )

    run_id: CleanStr = Field(
        description="Unique UUID string identifying this pipeline run."
    )
    correlation_id: CleanStr = Field(
        description="Global distributed trace correlation token."
    )
    tenant_id: CleanStr = Field(
        description="Isolated tenant workspace context identifier."
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Execution initialization UTC timestamp.",
    )
