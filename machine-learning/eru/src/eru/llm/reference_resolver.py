"""Coreference resolver using Outlines to group duplicate text entity mentions."""

from __future__ import annotations

import json
from typing import Any

import structlog

from eru.common.types import ExtractedCandidate, ReferenceResolution
from eru.llm.outlines_base import OutlinesGenerator

logger = structlog.get_logger(__name__)


class OutlinesReferenceResolver(OutlinesGenerator):
    """Uses LLM-guided generation via Outlines to perform coreference resolution.

    Attributes:
        model: The structural backend LLM instance.
        max_new_tokens: Maximum number of tokens permitted for each completion.
    """

    def __init__(self, model: Any, max_new_tokens: int = 1024):
        """Initializes the coreference resolver backend."""
        super().__init__(model=model, max_new_tokens=max_new_tokens)

    def resolve(
        self,
        text: str,
        candidates: list[ExtractedCandidate],
    ) -> ReferenceResolution:
        """Groups split text mentions into unified, single-entity coreference clusters.

        Args:
            text: The source raw text context.
            candidates: Initial raw entity candidates tracking internal mentions.

        Returns:
            A ReferenceResolution container highlighting verified coreference clusters.
        """
        log = logger.bind(
            text_length=len(text),
            incoming_candidates_count=len(candidates),
        )
        log.info("coreference_resolution_started")

        serialized_mentions = [
            {
                "text": mention.text,
                "labels": candidate.labels,
                "start_char": mention.start_char,
                "end_char": mention.end_char,
            }
            for candidate in candidates
            for mention in candidate.mentions
        ]

        log.debug(
            "flattened_mentions_for_payload",
            total_mentions_count=len(serialized_mentions),
        )

        system = (
            "You are an expert coreference resolution system.\n\n"
            "Task: Group disparate mentions that refer to the same real-world entity.\n\n"
            "Rules:\n"
            "- canonical_mention: Must be selected exactly from one of the provided mentions.\n"
            "- Consistency: Match text, aliases, pronouns, and absolute char spans exactly.\n"
            "- Restrictions: Do not invent mentions or entities. Do not merge if uncertain."
        )

        mentions_json = json.dumps(
            serialized_mentions, indent=2, ensure_ascii=False
        )
        user = f"TEXT:\n{text}\n\nENTITY MENTIONS:\n{mentions_json}"

        prompt = self.system_user_prompt(system, user)

        resolution_result = self.generate(prompt, ReferenceResolution)
        log.info("coreference_resolution_completed")
        return resolution_result
