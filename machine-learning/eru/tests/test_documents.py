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


def test_open_relations_use_local_entities_and_can_add_grounded_entities() -> (
    None
):
    from eru.documents import OpenInformationPipeline

    class OpenBackend(Backend):
        def __call__(
            self,
            prompt: str,
            schema: type[BaseModel],
            max_new_tokens: int = 1024,
        ) -> object:
            if schema.__name__ == "StructuredExtractionResult":
                return super().__call__(prompt, schema, max_new_tokens)
            self.calls += 1
            assert "Alice" in prompt
            return {
                "facts": [
                    {
                        "subject": "Alice",
                        "subject_type": "PERSON",
                        "predicate": "visits",
                        "object": "Paris",
                        "object_type": "LOCATION",
                        "evidence": "Alice visits Paris",
                    },
                    {
                        "subject": "Invented",
                        "subject_type": "PERSON",
                        "predicate": "visits",
                        "object": "Paris",
                        "object_type": "LOCATION",
                        "evidence": "Alice visits Paris",
                    },
                ]
            }

    backend = OpenBackend()
    result = OpenInformationPipeline(backend).extract(
        "Alice works in Lorraine. Alice visits Paris"
    )
    assert backend.calls == 2
    assert [
        (fact.subject, fact.predicate, fact.object) for fact in result.facts
    ] == [("Alice", "visits", "Paris")]
    assert result.rejected == 1


def test_registry_ontology_restricts_predicates_and_endpoint_types() -> None:
    from eru.documents import (
        ExtractionPolicy,
        OpenFacts,
        OpenInformationPipeline,
    )
    from galadril_ontology import Ontology, OntologyResource, ResourceKind

    ontology = Ontology(
        version="published-v1",
        resources=(
            OntologyResource(
                resource_id="object.person",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Person",
            ),
            OntologyResource(
                resource_id="object.place",
                kind=ResourceKind.OBJECT_TYPE,
                display_name="Place",
            ),
            OntologyResource(
                resource_id="link.visits",
                kind=ResourceKind.LINK_TYPE,
                display_name="visits",
                attributes={
                    "source_type": "object.person",
                    "target_type": "object.place",
                },
            ),
        ),
    )
    policy = ExtractionPolicy.from_registry(ontology)
    facts = OpenFacts.model_validate(
        {
            "facts": [
                {
                    "subject": "Alice",
                    "subject_type": "object.person",
                    "predicate": "link.visits",
                    "object": "Paris",
                    "object_type": "object.place",
                    "evidence": "Alice visits Paris",
                },
                {
                    "subject": "Alice",
                    "subject_type": "object.place",
                    "predicate": "link.visits",
                    "object": "Paris",
                    "object_type": "object.person",
                    "evidence": "Alice visits Paris",
                },
                {
                    "subject": "Alice",
                    "subject_type": "object.person",
                    "predicate": "invented",
                    "object": "Paris",
                    "object_type": "object.place",
                    "evidence": "Alice visits Paris",
                },
            ]
        }
    )
    result = OpenInformationPipeline.project(
        "Alice visits Paris", (), facts, policy
    )
    assert len(result.facts) == 1
    assert result.facts[0].predicate == "link.visits"
    assert result.rejected == 2


def test_empty_registry_ontology_never_becomes_unrestricted() -> None:
    from eru.documents import (
        ExtractionPolicy,
        OpenFacts,
        OpenInformationPipeline,
    )
    from galadril_ontology import Ontology

    policy = ExtractionPolicy.from_registry(Ontology(version="empty"))
    proposed = OpenFacts.model_validate(
        {
            "facts": [
                {
                    "subject": "Alice",
                    "subject_type": "PERSON",
                    "predicate": "visits",
                    "object": "Paris",
                    "object_type": "LOCATION",
                    "evidence": "Alice visits Paris",
                }
            ]
        }
    )
    result = OpenInformationPipeline.project(
        "Alice visits Paris", (), proposed, policy
    )
    assert not result.facts
    assert result.rejected == 1


def test_pipeline_consumes_task_bound_registry_ontology_without_global_state() -> (
    None
):
    import asyncio
    from datetime import UTC, datetime

    from eru.documents import OpenInformationPipeline
    from galadril_ontology import (
        Ontology,
        OntologyRuntimeManager,
        OntologySlice,
        OntologySliceMetadata,
        OntologySliceRequest,
    )

    class Store:
        async def load_runtime_slice(
            self, request: OntologySliceRequest
        ) -> OntologySlice:
            return OntologySlice(
                ontology=Ontology(version="closed"),
                metadata=OntologySliceMetadata(
                    tenant_id=request.tenant_id,
                    ontology_id="ontology",
                    publication_id="published",
                    revision_id="a" * 32,
                    base_version="1",
                    base_hash="b" * 64,
                    effective_hash="c" * 64,
                    published_at=datetime.now(UTC),
                ),
            )

    backend = Backend()

    async def extract() -> None:
        request = OntologySliceRequest(
            tenant_id="tenant-a",
            pipeline_id="pipeline",
            pipeline_revision_id="revision",
            block_id="extract",
        )
        async with OntologyRuntimeManager(Store()).bind(request):
            result = OpenInformationPipeline(backend).extract(
                "Alice works in Lorraine"
            )
            assert result.facts == ()
            assert backend.calls == 0
        assert OpenInformationPipeline(backend).entities(
            "Alice works in Lorraine"
        )
        assert backend.calls == 1

    asyncio.run(extract())


if __name__ == "__main__":
    sys.exit(pytest.main(["--import-mode=importlib", __file__]))
