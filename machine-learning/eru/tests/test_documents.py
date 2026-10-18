"""Document extraction reuses Eru while keeping hypotheses source-grounded."""

import sys

import pytest
from eru.documents import DocumentPipeline
from pydantic import BaseModel


class Backend:
    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, prompt: str, schema: type[BaseModel], max_new_tokens: int = 1024
    ) -> object:
        self.calls += 1
        if schema.__name__ == "StructuredExtractionResult":
            return {
                "entities": [
                    {
                        "text": "Alice",
                        "labels": ["PERSON"],
                        "start_char": 0,
                        "end_char": 5,
                        "confidence": 0.9,
                    },
                    {
                        "text": "Lorraine",
                        "labels": ["LOCATION"],
                        "start_char": 15,
                        "end_char": 23,
                        "confidence": 0.9,
                    },
                ]
            }
        return {
            "relations": [
                {
                    "source_id": "ent_0",
                    "target_id": "ent_1",
                    "relation_type": "works in",
                    "evidence": "Alice works in Lorraine",
                },
                {
                    "source_id": "ent_1",
                    "target_id": "invented",
                    "relation_type": "owns",
                    "evidence": "Alice works in Lorraine",
                },
            ]
        }


def test_three_stage_projection_uses_two_bounded_generations_and_rejects_unknown_nodes() -> (
    None
):
    backend = Backend()
    result = DocumentPipeline(backend).extract("Alice works in Lorraine")
    assert backend.calls == 2
    assert [
        (fact.subject, fact.predicate, fact.object) for fact in result.facts
    ] == [("Alice", "works in", "Lorraine")]
    assert result.rejected == 1


def test_document_budget_is_checked_before_model_execution() -> None:
    backend = Backend()
    with pytest.raises(ValueError):
        DocumentPipeline(backend).extract("x" * 16385)
    assert backend.calls == 0


def test_missing_entities_can_use_a_grounded_local_projection() -> None:
    class Incomplete:
        def __call__(
            self,
            prompt: str,
            schema: type[BaseModel],
            max_new_tokens: int = 1024,
        ) -> object:
            if schema.__name__ == "StructuredExtractionResult":
                return {"entities": []}
            return {
                "facts": [
                    {
                        "subject": "Alice",
                        "predicate": "works in",
                        "object": "Lorraine",
                        "evidence": "Alice works in Lorraine",
                    },
                    {
                        "subject": "Invented",
                        "predicate": "owns",
                        "object": "Lorraine",
                        "evidence": "Alice works in Lorraine",
                    },
                ]
            }

    result = DocumentPipeline(Incomplete()).extract("Alice works in Lorraine")
    assert len(result.facts) == 1
    assert result.facts[0].subject == "Alice"
    assert result.rejected == 1


def test_unambiguous_copied_mentions_can_repair_model_offsets() -> None:
    class Inaccurate(Backend):
        def __call__(
            self,
            prompt: str,
            schema: type[BaseModel],
            max_new_tokens: int = 1024,
        ) -> object:
            result = super().__call__(prompt, schema, max_new_tokens)
            if (
                isinstance(result, dict)
                and schema.__name__ == "StructuredExtractionResult"
            ):
                result["entities"][1]["start_char"] = 12
            return result

    assert (
        DocumentPipeline(Inaccurate()).extract("Alice works in Lorraine").facts
    )


if __name__ == "__main__":
    sys.exit(pytest.main(["--import-mode=importlib", __file__]))
