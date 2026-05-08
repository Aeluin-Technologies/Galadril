"""Outlines-based semantic reasoner for Layer 2."""

import logging
from typing import Any, get_args

from pydantic import ValidationError, create_model

from eru.exceptions import ReasoningError
from eru.types import ExtractedCandidate, RelationDef, TGraph

logger = logging.getLogger(__name__)


class OutlinesReasoner:
    """Uses Outlines to constrain LLM generation to ONLY relations, reconstructing the graph algorithmically."""

    def __init__(
        self,
        model: Any,
        relation_defs: list[RelationDef] | None = None,
        open_entity_types: list[str] | None = None,
        entity_mapping: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self.relation_defs = relation_defs or []
        self.open_entity_types = open_entity_types or []
        self.entity_mapping = entity_mapping or {
            "id": "id",
            "text": "text",
            "label": "type",
        }

    def reason(
        self,
        text: str,
        candidates: list[ExtractedCandidate],
        schema: type[TGraph],
    ) -> TGraph:
        try:
            relations_type = schema.model_fields["relations"].annotation
            entity_type_annotation = schema.model_fields["entities"].annotation
            entity_class = get_args(entity_type_annotation)[0]
        except KeyError as e:
            raise ReasoningError(
                f"User schema must have 'entities' and 'relations' fields: {e}"
            )

        LLMRelationsModel = create_model(
            "LLMRelationsModel", relations=(relations_type, ...)
        )

        if not candidates and not self.open_entity_types:
            return schema(entities=[], relations=[])

        entities_str = "\n".join(
            f"- ID: 'ent_{i}' | Text: '{c.text}' | Type: '{c.label}'"
            for i, c in enumerate(candidates)
        )

        system_prompt = (
            "You are an expert Knowledge Graph extraction system.\n"
            "Your ONLY task is to extract valid relationships between entities based on the text.\n"
            "1. For provided entities, ALWAYS use their exact 'ID' (e.g., 'ent_0') in the relation's source or target field."
            "You MUST respect logical direction between entities (sources and targets) (e.g. a house CANNOT live in a man; but a man lives in a house.)\n"
        )

        if self.open_entity_types:
            allowed_open = ", ".join(f"'{t}'" for t in self.open_entity_types)
            system_prompt += (
                f"2. For conceptual/implicit elements of type {allowed_open} (like intents or reasons), "
                "write the concept's description DIRECTLY into the relation's source or target field "
                "(e.g., 'to gather intelligence') instead of an ID.\n"
            )

        if self.relation_defs:
            system_prompt += "\nALLOWED RELATION TYPES & DEFINITIONS:\n"
            for r_def in self.relation_defs:
                system_prompt += f"- '{r_def.name}': {r_def.description}\n"
                if r_def.examples:
                    system_prompt += (
                        f"  Examples: {', '.join(r_def.examples)}\n"
                    )

        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            "<|im_start|>user\n"
            f"TEXT:\n{text}\n\n"
            f"PROVIDED ENTITIES:\n{entities_str}\n"
            "<|im_end|>\n"
            "<|im_start|>assistant\n"
        )

        try:
            llm_result = self.model(
                prompt, LLMRelationsModel, max_new_tokens=1024
            )

            if isinstance(llm_result, str):
                parsed_relations = LLMRelationsModel.model_validate_json(
                    llm_result
                ).relations
            elif isinstance(llm_result, dict):
                parsed_relations = LLMRelationsModel.model_validate(
                    llm_result
                ).relations
            elif hasattr(llm_result, "relations"):
                parsed_relations = llm_result.relations
            else:
                parsed_relations = LLMRelationsModel.model_validate(
                    llm_result
                ).relations

        except ValidationError as e:
            raise ReasoningError(
                f"LLM relations output violated the Pydantic schema: {e}"
            ) from e
        except Exception as e:
            raise ReasoningError(f"Outlines generation failed: {e}") from e

        reconstructed_entities = []
        for i, c in enumerate(candidates):
            ent_kwargs = {
                self.entity_mapping["id"]: f"ent_{i}",
                self.entity_mapping["text"]: c.text,
                self.entity_mapping["label"]: c.label,
            }
            reconstructed_entities.append(entity_class(**ent_kwargs))

        return schema(
            entities=reconstructed_entities, relations=parsed_relations
        )
