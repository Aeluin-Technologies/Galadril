"""Semantic normalizer using Outlines to canonicalize entities and ground them to an ontology."""

from __future__ import annotations

import json
from typing import Any

import structlog

from eru.common.types import (
    ExtractedCandidate,
    ReferenceResolution,
    SemanticNormalization,
)
from eru.llm.outlines_base import OutlinesGenerator

logger = structlog.get_logger(__name__)


class OutlinesSemanticNormalizer(OutlinesGenerator):
    """Uses LLM-guided generation via Outlines to standardize and resolve entities.

    Attributes:
        model: The structural backend LLM instance.
        max_new_tokens: Maximum number of tokens permitted for each completion.
    """

    def __init__(self, model: Any, max_new_tokens: int = 1024):
        """Initializes the semantic normalizer backend."""
        super().__init__(model=model, max_new_tokens=max_new_tokens)

    def normalize(
        self,
        text: str,
        candidates: list[ExtractedCandidate],
        references: ReferenceResolution,
    ) -> SemanticNormalization:
        """Resolves coreference clusters and candidates into precise canonical entities.

        Args:
            text: The source raw text context.
            candidates: Initial raw entity candidates from the extraction phase.
            references: Coreference clusters tracking mentions of the same entity.

        Returns:
            The schema-compliant SemanticNormalization containing grounded entities.
        """
        log = logger.bind(
            text_length=len(text),
            candidates_count=len(candidates),
        )
        log.info("semantic_normalization_started")

        system = (
            "You are an expert ontology grounding system.\n\n"
            "Task: Resolve coreference clusters into clean, canonical entities.\n\n"
            "Rules:\n"
            "- canonical_name: Use the most standard, widely recognized name.\n"
            "- aliases: Must be derived exactly from provided mentions (do not invent).\n"
            "- canonical_label: Assign the most precise semantic type available.\n"
            "- Restrictions: Never invent entities or merge distinct real-world items."
        )

        ref_json = references.model_dump_json(indent=2)
        cand_payload = [x.model_dump() for x in candidates]
        cand_json = json.dumps(cand_payload, indent=2, ensure_ascii=False)

        log.debug(
            "serialized_normalization_contexts",
            references_payload_bytes=len(ref_json),
            candidates_payload_bytes=len(cand_json),
        )

        user = (
            f"TEXT:\n{text}\n\n"
            f"COREFERENCE:\n{ref_json}\n\n"
            f"EXTRACTED:\n{cand_json}"
        )

        prompt = self.system_user_prompt(system, user)

        normalization_result = self.generate(prompt, SemanticNormalization)
        log.info("semantic_normalization_completed")
        return normalization_result
