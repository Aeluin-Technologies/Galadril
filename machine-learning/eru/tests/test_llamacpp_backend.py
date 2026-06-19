"""Tests for the llama.cpp JSON adapter."""

from __future__ import annotations

from pydantic import BaseModel
import pytest

from eru.common.exceptions import ReasoningError, ModelResolutionError
from eru.llm.llamacpp import LlamaCppConfig, LlamaCppJsonModel


class Payload(BaseModel):
    """Small structured payload used to validate JSON parsing."""

    value: int


class FakeLlama:
    """Minimal llama.cpp-compatible callable for deterministic tests."""

    __slots__ = ("response", "calls")

    def __init__(self, response: object) -> None:
        """Stores a response payload without allocating runtime model state."""
        self.response = response
        self.calls = 0

    def __call__(self, prompt: str, **_: object) -> object:
        """Records the prompt and returns the configured fake completion."""
        self.calls += 1
        assert "JSON Schema" in prompt
        return self.response


def test_llamacpp_json_model_validates_json_payload() -> None:
    model = LlamaCppJsonModel(
        FakeLlama({"choices": [{"text": '{"value": 7}'}]})
    )

    assert model("prompt", Payload, max_new_tokens=8) == {"value": 7}


def test_llamacpp_json_model_extracts_wrapped_json() -> None:
    model = LlamaCppJsonModel(
        FakeLlama({"choices": [{"text": 'Here is the result:\n{"value": 3}'}]})
    )

    assert model("prompt", Payload, max_new_tokens=8) == {"value": 3}


def test_llamacpp_json_model_rejects_invalid_schema() -> None:
    model = LlamaCppJsonModel(
        FakeLlama({"choices": [{"text": '{"value": "bad"}'}]})
    )

    with pytest.raises(ReasoningError):
        model("prompt", Payload, max_new_tokens=8)


def test_llamacpp_config_requires_model_path_to_load() -> None:
    with pytest.raises(ModelResolutionError):
        LlamaCppJsonModel.from_config(LlamaCppConfig())
