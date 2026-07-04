"""Tests for the underlying llama.cpp JSON parsing adapter layer."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from eru.common.exceptions import ReasoningError, ModelResolutionError
from eru.llm.llamacpp import LlamaCppJsonModel, LlamaCppConfig


class SimpleSchema(BaseModel):
    id: str


def test_llamacpp_model_parses_markdown_wrapped_json() -> None:
    """Validates markdown parsing stripping tolerances."""

    def fake_llama(*args, **kwargs) -> dict:
        return {"choices": [{"text": '```json\n{"id": "test_id"}\n```'}]}

    adapter = LlamaCppJsonModel(llama=fake_llama)
    res = adapter("prompt", SimpleSchema)
    assert res == {"id": "test_id"}


def test_llamacpp_model_wraps_raw_lists_in_relations_key() -> None:
    """Validates automatic conversion fallback for schema arrays."""

    def fake_llama(*args, **kwargs) -> dict:
        return {"choices": [{"text": '[{"source_id": "e1"}]'}]}

    class MockRelationsSchema(BaseModel):
        relations: list[dict]

    adapter = LlamaCppJsonModel(llama=fake_llama)
    res = adapter("prompt", MockRelationsSchema)
    assert "relations" in res


def test_llamacpp_model_raises_reasoning_error_on_invalid_json() -> None:
    """Ensures structural formatting errors raise ReasoningError."""

    def fake_llama(*args, **kwargs) -> dict:
        return {"choices": [{"text": "Corrupted non JSON data"}]}

    adapter = LlamaCppJsonModel(llama=fake_llama)
    with pytest.raises(ReasoningError):
        adapter("prompt", SimpleSchema)


def test_llamacpp_config_missing_path() -> None:
    """Ensures initialization without paths fails safely."""
    with pytest.raises(ModelResolutionError):
        LlamaCppJsonModel.from_config(LlamaCppConfig(model_path=None))
