"""Structured reasoner utilizing Outlines for schema-guided relation extraction."""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, create_model

from eru.common.types import CanonicalEntity, RelationCandidate
from eru.llm.outlines_base import OutlinesGenerator
from eru.schema import GraphSchema

logger = structlog.get_logger(__name__)


class OutlinesReasoner(OutlinesGenerator):
    """Uses LLM guided generation via Outlines to reason and extract graph relations.

    Attributes:
        model: The structural backend LLM instance.
        max_new_tokens: Maximum number of tokens permitted for each completion.
    """

    def __init__(self, model: Any, max_new_tokens: int = 256):
        """Initializes the structured reasoner backend."""
        super().__init__(model=model, max_new_tokens=max_new_tokens)

    def reason(
        self,
        text: str,
        entities: list[CanonicalEntity],
        candidates: list[RelationCandidate],
        schema: GraphSchema,
    ) -> BaseModel:
        """Evaluates relation candidates through guided inference to construct a graph.

        Args:
            text: The source raw text.
            entities: Evaluated structural canonical entities.
            candidates: Permitted relationship directional linkages.
            schema: The target global graph blueprint schema.

        Returns:
            The populated Pydantic GraphModel containing valid entities and relations.
        """
        log = logger.bind(
            incoming_entities_count=len(entities),
            incoming_candidates_count=len(candidates),
            schema_name=schema.__class__.__name__,
        )
        log.info("outlines_relation_reasoning_started")

        entity_index = {f"ent_{i}": entity for i, entity in enumerate(entities)}

        log.debug("building_dynamic_relation_output_schema")
        relation_schema = create_model(
            "RelationOutput",
            relations=(list[schema.relation_model], ...),
        )

        relations = []
        seen = set()
        system = self._build_system_prompt(schema)

        failed_generations_count = 0
        skipped_duplicate_relations = 0

        for i, candidate in enumerate(candidates):
            source = entity_index[candidate.source_id]
            target = entity_index[candidate.target_id]
            user = self._build_user_prompt(text, candidate, source, target)

            iter_log = logger.bind(
                candidate_index=i,
                source_id=candidate.source_id,
                source_name=source.canonical_name,
                target_id=candidate.target_id,
                target_name=target.canonical_name,
            )

            try:
                iter_log.debug("generating_relation_for_candidate_pair")
                result = self.generate(
                    self.system_user_prompt(system, user),
                    relation_schema,
                )
            except Exception:
                failed_generations_count += 1
                iter_log.exception("relation_generation_failed_for_candidate")
                continue

            iter_log.debug(
                "relation_generation_succeeded",
                relations_extracted_count=len(result.relations),
            )

            for relation in result.relations:
                key = (
                    relation.source_id,
                    relation.target_id,
                    relation.relation_type,
                )
                if key in seen:
                    skipped_duplicate_relations += 1
                    iter_log.debug(
                        "duplicate_relation_ignored",
                        relation_type=relation.relation_type,
                    )
                    continue

                seen.add(key)
                relations.append(relation)

        log.info(
            "outlines_relation_reasoning_completed",
            final_extracted_relations_count=len(relations),
            failed_generations_count=failed_generations_count,
            skipped_duplicate_relations=skipped_duplicate_relations,
        )

        return schema.graph_model(
            entities=self._build_entities(entities, schema),
            relations=relations,
        )

    def _build_entities(
        self,
        entities: list[CanonicalEntity],
        schema: GraphSchema,
    ) -> list[BaseModel]:
        """Converts internal canonical entities into the schema's concrete model.

        Args:
            entities: Discovered canonical entities.
            schema: Graph configuration context.

        Returns:
            A list of instantiated schema-compliant entity models.
        """
        model = schema.entity_model
        logger.debug(
            "mapping_canonical_entities_to_graph_model",
            entity_count=len(entities),
            target_entity_model=model.__name__,
        )

        result = []
        for i, entity in enumerate(entities):
            data = {
                "id": f"ent_{i}",
                "text": entity.canonical_name,
                "type": entity.labels[0],
            }
            result.append(model(**data))
        return result

    def _build_system_prompt(self, schema: GraphSchema) -> str:
        """Constructs an expert extraction system instructions prompt string.

        Args:
            schema: Graph definitions and constraint definitions.

        Returns:
            The complete system instructions text block.
        """
        base_prompt = (
            "You are an expert relation extraction system.\n\n"
            "Goal:\n"
            "Determine whether the two entities are connected.\n\n"
            "Rules:\n"
            "- Use only provided IDs.\n"
            "- Never invent entities.\n"
            "- Never invent IDs.\n"
            "- Always return a JSON object with a 'relations' key containing a list of relations.\n"
            '- If no relation exists, return {"relations": []}.\n'
            "- Respect relation direction.\n"
            "- Extract only relations explicitly supported by the text.\n\n"
            "Allowed relations:\n"
        )

        relation_blocks = []
        for relation in schema.relation_defs:
            block = f"\nRelation:\n{relation.name}\n\nDescription:\n{relation.description}\n"
            if relation.examples:
                examples_str = "\nExamples:\n" + "".join(
                    f"- {ex}\n" for ex in relation.examples
                )
                block += examples_str
            relation_blocks.append(block)

        constructed_prompt = base_prompt + "".join(relation_blocks)
        logger.debug(
            "system_prompt_built",
            relation_defs_count=len(schema.relation_defs),
            character_count=len(constructed_prompt),
        )
        return constructed_prompt

    def _build_user_prompt(
        self,
        text: str,
        candidate: RelationCandidate,
        source: CanonicalEntity,
        target: CanonicalEntity,
    ) -> str:
        """Builds a contextual evaluation prompt block for an individual link pair.

        Args:
            text: Context document body.
            candidate: Node connection blueprint containing IDs.
            source: Complete structural metadata for the source node.
            target: Complete structural metadata for the target node.

        Returns:
            The formatted multi-entity contextual question string.
        """
        source_labels = "".join(f"- {label}\n" for label in source.labels)
        target_labels = "".join(f"- {label}\n" for label in target.labels)

        return (
            f"TEXT:\n\n{text}\n\n"
            f"SOURCE ENTITY:\n\n"
            f"ID:\n{candidate.source_id}\n"
            f"Canonical:\n{source.canonical_name}\n"
            f"Labels:\n{source_labels}\n"
            f"TARGET ENTITY:\n\n"
            f"ID:\n{candidate.target_id}\n"
            f"Canonical:\n{target.canonical_name}\n"
            f"Labels:\n{target_labels}"
        )
