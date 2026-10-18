"""Tests for the underlying llama.cpp JSON parsing adapter layer."""

from __future__ import annotations

import sys

import pytest
from eru.common.exceptions import ModelResolutionError, ReasoningError
from eru.llm.llamacpp import LlamaCppConfig, LlamaCppJsonModel
from pydantic import BaseModel, Field


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


def test_schema_errors_do_not_expose_document_content_in_domain_errors() -> (
    None
):
    adapter = LlamaCppJsonModel(
        llama=lambda *args, **kwargs: {
            "choices": [{"text": '{"id":{"secret":"private-document"}}'}]
        }
    )
    with pytest.raises(ReasoningError) as failure:
        adapter("private-document", SimpleSchema)
    assert "private-document" not in str(failure.value)


def test_native_generation_enforces_grammar_and_resets_context() -> None:
    calls: list[str] = []
    received: dict[str, object] = {}

    def completion(prompt: str | list[int], **kwargs: object) -> object:
        received.update(kwargs)
        calls.append("generate")
        return {"choices": [{"text": '{"id":"valid"}'}]}

    adapter = LlamaCppJsonModel(
        llama=completion,
        grammar_factory=lambda schema: "grammar",
        reset_context=lambda: calls.append("reset"),
    )
    assert adapter("untrusted", SimpleSchema) == {"id": "valid"}
    assert calls == ["reset", "generate"]
    assert received["grammar"] == "grammar"


def test_large_string_bounds_do_not_expand_the_native_grammar() -> None:
    grammars: list[str] = []

    class Evidence(BaseModel):
        quote: str = Field(max_length=2048)

    adapter = LlamaCppJsonModel(
        llama=lambda *args, **kwargs: '{"quote":"source"}',
        grammar_factory=lambda schema: grammars.append(schema),
    )
    assert adapter("evidence", Evidence) == {"quote": "source"}
    assert "maxLength" not in grammars[0]


if __name__ == "__main__":
    sys.exit(pytest.main(["--import-mode=importlib", __file__]))
