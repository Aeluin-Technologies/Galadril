"""LLM-based candidate extraction using an existing structured generation backend."""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field

from eru.common.exceptions import ExtractionError
from eru.llm.outlines_base import OutlinesGenerator
from eru.common.types import EntityMention, ExtractedCandidate

logger = structlog.get_logger(__name__)


class StructuredMention(BaseModel):
    """Schema for one entity mention produced by the shared LLM backend."""

    text: str
    labels: list[str] = Field(default_factory=list)
    start_char: int
    end_char: int
    confidence: float = 1.0


class StructuredExtractionResult(BaseModel):
    """Container schema for LLM-based candidate extraction."""

    entities: list[StructuredMention] = Field(default_factory=list)


class StructuredCandidateExtractor(OutlinesGenerator):
    """Extracts entity candidates with the same LLM used by the rest of Eru."""

    def __init__(
        self,
        model: Any,
        labels: list[str],
        max_new_tokens: int = 1024,
    ) -> None:
        """Initializes extraction with caller-owned labels and a shared model."""
        super().__init__(model=model, max_new_tokens=max_new_tokens)
        self.labels = labels
        self._label_set = frozenset(labels)

        logger.info(
            "structured_candidate_extractor_initialized",
            label_count=len(labels),
            max_new_tokens=max_new_tokens,
        )

    def extract(self, text: str) -> list[ExtractedCandidate]:
        """Extracts candidates without requiring a second downloaded model."""
        text_length = len(text)
        if not text.strip():
            logger.debug(
                "structured_extraction_skipped_empty_text",
                text_length=text_length,
            )
            return []

        log = logger.bind(text_length=text_length)
        log.info("structured_extraction_started")

        system = (
            "You are an expert entity extraction system.\n\n"
            "Task: Extract explicit entity mentions from the text.\n\n"
            "Rules:\n"
            "- Use only the allowed labels.\n"
            "- text must be copied exactly from the source text.\n"
            "- start_char and end_char must be zero-based character offsets.\n"
            "- Do not invent entities or implicit concepts."
        )
        label_lines = "".join(f"- {label}\n" for label in self.labels)
        user = f"ALLOWED LABELS:\n{label_lines}\nTEXT:\n{text}"

        try:
            log.debug("dispatching_llm_generation_call")
            result = self.generate(
                self.system_user_prompt(system, user),
                StructuredExtractionResult,
            )
            log.debug(
                "llm_generation_completed",
                raw_mentions_count=len(result.entities),
            )
        except Exception as exc:
            log.exception("structured_generation_failed")
            raise ExtractionError(
                f"Structured extraction failed: {exc}"
            ) from exc

        return self._to_candidates(text, result.entities)

    def _to_candidates(
        self,
        source_text: str,
        mentions: list[StructuredMention],
    ) -> list[ExtractedCandidate]:
        """Converts validated LLM mentions into internal extraction records."""
        candidates: list[ExtractedCandidate] = []
        seen: set[tuple[str, int, int]] = set()

        # Performance/Validation tracking telemetry counters
        skipped_invalid_span = 0
        skipped_invalid_labels = 0
        skipped_duplicates = 0

        for mention in mentions:
            if not self._has_valid_span(source_text, mention):
                skipped_invalid_span += 1
                continue

            labels = [
                label for label in mention.labels if label in self._label_set
            ]
            if not labels:
                skipped_invalid_labels += 1
                continue

            key = (mention.text, mention.start_char, mention.end_char)
            if key in seen:
                skipped_duplicates += 1
                continue
            seen.add(key)

            score = min(max(mention.confidence, 0.0), 1.0)
            entity_mention = EntityMention(
                text=mention.text,
                start_char=mention.start_char,
                end_char=mention.end_char,
                score=score,
            )
            candidates.append(
                ExtractedCandidate(
                    text=mention.text,
                    labels=labels,
                    mentions=[entity_mention],
                    confidence=score,
                )
            )

        logger.info(
            "structured_mentions_parsing_completed",
            final_candidates_count=len(candidates),
            skipped_invalid_span=skipped_invalid_span,
            skipped_invalid_labels=skipped_invalid_labels,
            skipped_duplicates=skipped_duplicates,
        )
        return candidates

    def _has_valid_span(
        self,
        source_text: str,
        mention: StructuredMention,
    ) -> bool:
        """Checks offsets and exact text equality before accepting a mention."""
        if mention.start_char < 0 or mention.end_char <= mention.start_char:
            logger.debug(
                "invalid_span_indices",
                start=mention.start_char,
                end=mention.end_char,
            )
            return False
        if mention.end_char > len(source_text):
            logger.debug(
                "span_exceeds_text_length",
                end=mention.end_char,
                text_len=len(source_text),
            )
            return False

        is_exact_match = (
            source_text[mention.start_char : mention.end_char] == mention.text
        )
        if not is_exact_match:
            logger.debug(
                "span_text_mismatch",
                expected=mention.text,
                actual=source_text[mention.start_char : mention.end_char],
            )
        return is_exact_match
