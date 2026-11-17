"""Unit tests for durable pipeline event contracts."""

from datetime import datetime
from uuid import uuid4

import pytest
from galadril_pipeline.config import StepType
from galadril_pipeline.events import PipelineCommand, ResourceClass
from pydantic import ValidationError


def test_command_round_trip_preserves_identity() -> None:
    """Ensures commands remain stable across Kafka JSON serialization."""
    correlation_id = uuid4()
    command = PipelineCommand(
        correlation_id=correlation_id,
        pipeline="vision",
        entity_id="entity-7",
        step="infer",
        step_type=StepType.INFERENCE,
        resource_class=ResourceClass.GPU,
        payload={"storage_path": "s3://bucket/object"},
    )

    restored = PipelineCommand.model_validate_json(command.model_dump_json())

    assert restored == command
    assert restored.correlation_id == correlation_id
    assert restored.idempotency_key == f"{command.event_id}:vision:infer"


def test_admission_is_bounded_and_never_inferred_from_attributes() -> None:
    """Keeps host-issued authority separate from untrusted pipeline metadata."""
    common = {
        "correlation_id": uuid4(),
        "pipeline": "vision",
        "step": "infer",
        "step_type": StepType.INFERENCE,
        "resource_class": ResourceClass.GPU,
    }
    command = PipelineCommand(**common, admission="opaque-host-admission")
    restored = PipelineCommand.model_validate_json(command.model_dump_json())
    assert restored.admission == "opaque-host-admission"
    assert "opaque-host-admission" not in repr(command)
    assert PipelineCommand(
        **common, attributes={"admission": "forged"}
    ).admission is None
    for invalid in ("", " ", "x" * 4097):
        with pytest.raises(ValidationError):
            PipelineCommand(**common, admission=invalid)


def test_command_rejects_naive_timestamp_and_unknown_fields() -> None:
    """Prevents ambiguous timestamps and accidental contract drift."""
    common = {
        "correlation_id": uuid4(),
        "pipeline": "vision",
        "step": "infer",
        "step_type": StepType.INFERENCE,
        "resource_class": ResourceClass.GPU,
    }

    with pytest.raises(ValidationError, match="must include a timezone"):
        PipelineCommand(**common, occurred_at=datetime(2026, 1, 1))

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PipelineCommand(**common, undocumented=True)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
