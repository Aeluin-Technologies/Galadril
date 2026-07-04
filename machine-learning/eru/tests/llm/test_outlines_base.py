"""Tests for base Outlines structured generation core class logic."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from eru.common.exceptions import ReasoningError
from eru.llm.outlines_base import OutlinesGenerator


class SamplePayload(BaseModel):
    active: bool


def test_generate_handles_pydantic_instance_passthrough() -> None:
    """Verifies structural extraction returns direct model fields intact."""

    def model_returning_instance(*args, **kwargs):
        return SamplePayload(active=True)

    generator = OutlinesGenerator(model=model_returning_instance)
    result = generator.generate("prompt", SamplePayload)
    assert result.active is True


def test_generate_raises_reasoning_error_on_validation_failure() -> None:
    """Ensures unexpected payload schemas raise explicit reasoning errors."""

    def model_returning_invalid(*args, **kwargs):
        return {"active": "not-a-boolean"}

    generator = OutlinesGenerator(model=model_returning_invalid)
    with pytest.raises(ReasoningError):
        generator.generate("prompt", SamplePayload)
