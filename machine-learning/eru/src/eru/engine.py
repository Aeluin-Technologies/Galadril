"""Main orchestration pipeline for graph-based knowledge extraction and processing."""

from __future__ import annotations

from typing import Any

import structlog

from eru.common.exceptions import (
    ExtractionError,
    LogicValidationError,
    ReasoningError,
)
from eru.common.types import (
    CandidateExtractor,
    CoreferenceResolver,
    ImplicitEntityGenerator,
    LogicValidator,
    RelationCandidateGenerator,
    SemanticNormalizer,
    SemanticReasoner,
    TGraph,
)
from eru.schema import GraphSchema

logger = structlog.get_logger(__name__)


class EruEngine[TGraph]:
    """Orchestrates the pipeline for transforming text into a structured knowledge graph.

    Attributes:
        schema: The GraphSchema defining rules, models, and constraints.
        extractor: Component responsible for extracting raw entity candidates.
        reasoner: Core component that infers structured relationships.
        relation_candidates: Component that proposes relationship pairs.
        entity_merger: Component that unifies matching or overlapping entities.
        validator: Optional component to validate logical graph consistency.
        coreference: Optional component to resolve pronouns and text aliases.
        normalizer: Optional component to standardize entity semantics.
        implicit_entities: Optional component to infer unstated entity definitions.
    """

    def __init__(
        self,
        schema: GraphSchema,
        extractor: CandidateExtractor,
        reasoner: SemanticReasoner,
        relation_candidates: RelationCandidateGenerator,
        entity_merger: Any,
        validator: LogicValidator | None = None,
        coreference: CoreferenceResolver | None = None,
        normalizer: SemanticNormalizer | None = None,
        implicit_entities: ImplicitEntityGenerator | None = None,
    ):
        """Initializes the EruEngine with the required pipeline execution steps."""
        self.schema = schema
        self.extractor = extractor
        self.reasoner = reasoner
        self.relation_candidates = relation_candidates
        self.entity_merger = entity_merger
        self.validator = validator
        self.coreference = coreference
        self.normalizer = normalizer
        self.implicit_entities = implicit_entities

        logger.info(
            "eru_engine_pipeline_initialized",
            schema_type=schema.__class__.__name__,
            has_validator=validator is not None,
            has_coreference=coreference is not None,
            has_normalizer=normalizer is not None,
            has_implicit_entities=implicit_entities is not None,
        )

    def process(self, text: str) -> TGraph:
        """Processes raw text through the graph extraction and validation pipeline.

        Args:
            text: The raw input string to build the knowledge graph from.

        Returns:
            The fully realized, validated knowledge graph instance (TGraph).

        Raises:
            ValueError: If the input text is empty or purely whitespace.
            ExtractionError: If the entity extraction phase fails.
            ReasoningError: If the semantic reasoning graph construction fails.
            LogicValidationError: If the final graph fails schema business rules.
        """
        text_length = len(text) if text else 0
        if not text or not text.strip():
            logger.error(
                "eru_engine_process_failed_empty_input", text_length=text_length
            )
            raise ValueError("Empty input.")

        log = logger.bind(text_length=text_length)
        log.info("eru_engine_pipeline_execution_started")

        log.info("pipeline_stage_1_entity_extraction_started")
        try:
            extracted = self.extractor.extract(text)
            log.info(
                "pipeline_stage_1_entity_extraction_completed",
                extracted_candidates_count=len(extracted),
            )
        except Exception as e:
            log.exception("pipeline_stage_1_entity_extraction_failed")
            raise ExtractionError(str(e)) from e

        references = None
        if self.coreference:
            log.info("pipeline_stage_2_coreference_resolution_started")
            references = self.coreference.resolve(text, extracted)
            log.info("pipeline_stage_2_coreference_resolution_completed")
        else:
            log.debug("pipeline_stage_2_coreference_resolution_skipped")

        normalization = None
        if self.normalizer and references:
            log.info("pipeline_stage_3_semantic_normalization_started")
            normalization = self.normalizer.normalize(
                text, extracted, references
            )
            log.info("pipeline_stage_3_semantic_normalization_completed")
        else:
            log.debug(
                "pipeline_stage_3_semantic_normalization_skipped",
                has_normalizer=self.normalizer is not None,
                has_references=references is not None,
            )

        log.info("pipeline_stage_4_entity_merging_started")
        entities = self.entity_merger.merge(
            extracted, references, normalization
        )
        log.info(
            "pipeline_stage_4_entity_merging_completed",
            canonical_entities_count=len(entities),
        )

        if self.implicit_entities:
            log.info("pipeline_stage_5_implicit_entity_generation_started")
            generated = self.implicit_entities.generate(text, entities)
            entities.extend(generated.entities)
            log.info(
                "pipeline_stage_5_implicit_entity_generation_completed",
                generated_entities_count=len(generated.entities),
                total_combined_entities_count=len(entities),
            )
        else:
            log.debug("pipeline_stage_5_implicit_entity_generation_skipped")

        log.info("pipeline_stage_6_relation_proposing_started")
        candidates = self.relation_candidates.propose(entities)
        log.info(
            "pipeline_stage_6_relation_proposing_completed",
            proposed_relation_candidates_count=len(candidates),
        )

        log.info("pipeline_stage_7_semantic_reasoning_started")
        try:
            graph = self.reasoner.reason(
                text, entities, candidates, self.schema
            )
            log.info("pipeline_stage_7_semantic_reasoning_completed")
        except Exception as e:
            log.exception("pipeline_stage_7_semantic_reasoning_failed")
            raise ReasoningError(str(e)) from e

        if self.validator:
            log.info("pipeline_stage_8_graph_logic_validation_started")
            try:
                graph = self.validator.validate(graph)
                log.info("pipeline_stage_8_graph_logic_validation_completed")
            except Exception as e:
                log.exception("pipeline_stage_8_graph_logic_validation_failed")
                raise LogicValidationError(str(e)) from e
        else:
            log.debug("pipeline_stage_8_graph_logic_validation_skipped")

        log.info("eru_engine_pipeline_execution_completed_successfully")
        return graph
