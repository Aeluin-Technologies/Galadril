"""Entity merger module responsible for aggregating disparate extractions into unified entities."""

from __future__ import annotations

from collections import defaultdict

import structlog

from eru.common.types import (
    CanonicalEntity,
    EntityMention,
    ExtractedCandidate,
    ReferenceResolution,
    SemanticNormalization,
)

logger = structlog.get_logger(__name__)


class DefaultEntityMerger:
    """Merges and unifies extracted candidate entities into definitive canonical objects.

    Uses normalized aliases and mapping rules to cluster candidates, aggregating
    their text mentions, entity types (labels), and associated metadata.
    """

    def merge(
        self,
        candidates: list[ExtractedCandidate],
        references: ReferenceResolution | None,
        normalization: SemanticNormalization | None,
    ) -> list[CanonicalEntity]:
        """Groups candidate extractions into clean, deduplicated CanonicalEntity instances.

        Args:
            candidates: Initial raw entity candidates found in the extraction phase.
            references: Coreference clusters tracking mention spans (reserved/unused).
            normalization: Grounded ontology data tracking canonical maps and labels.

        Returns:
            A list of consolidated, sorted, and unique CanonicalEntity objects.
        """
        log = logger.bind(
            incoming_candidates_count=len(candidates),
            has_normalization_rules=normalization is not None,
            has_references=references is not None,
        )
        log.info("entity_merge_process_started")

        alias_map = self._build_alias_map(normalization)
        label_map = self._build_label_map(normalization)

        groups = defaultdict(list)
        for candidate in candidates:
            canonical = alias_map.get(candidate.text, candidate.text)
            groups[canonical].append(candidate)

        log.debug(
            "candidates_clustered_into_canonical_groups",
            unique_groups_count=len(groups),
        )

        entities = []
        for canonical, group in groups.items():
            labels = set()
            mentions: list[EntityMention] = []
            confidence = 0.0
            metadata = {}

            for candidate in group:
                labels.update(candidate.labels)
                mentions.extend(candidate.mentions)
                confidence = max(confidence, candidate.confidence)
                metadata.update(candidate.metadata)

            normalized_label = label_map.get(canonical)
            if normalized_label:
                labels.add(normalized_label)

            mentions.sort(key=lambda x: x.start_char)
            metadata["mention_count"] = len(mentions)

            entities.append(
                CanonicalEntity(
                    canonical_name=canonical,
                    labels=sorted(labels),
                    mentions=mentions,
                    confidence=confidence,
                    metadata=metadata,
                )
            )

        log.info(
            "entity_merge_process_completed",
            final_canonical_entities_count=len(entities),
        )
        return entities

    def _build_alias_map(
        self, normalization: SemanticNormalization | None
    ) -> dict[str, str]:
        """Creates a lookup dictionary pointing variants and aliases back to a canonical name.

        Args:
            normalization: Grounded semantic normalizations container.

        Returns:
            A flat mapping dictionary mapping name variations to their canonical root string.
        """
        if normalization is None:
            return {}

        aliases = {}
        for entity in normalization.entities:
            canonical = entity.canonical_name
            aliases[canonical] = canonical
            for alias in entity.aliases:
                aliases[alias] = canonical

        logger.debug("alias_map_built", total_mapped_variants=len(aliases))
        return aliases

    def _build_label_map(
        self, normalization: SemanticNormalization | None
    ) -> dict[str, str]:
        """Creates a direct mapping linking a unique canonical entity name to its target label.

        Args:
            normalization: Grounded semantic normalizations container.

        Returns:
            A lookup dictionary mapping entity names to their preferred type label.
        """
        if normalization is None:
            return {}

        label_map = {
            entity.canonical_name: entity.canonical_label
            for entity in normalization.entities
        }

        logger.debug(
            "canonical_label_map_built",
            mapped_canonical_labels_count=len(label_map),
        )
        return label_map
