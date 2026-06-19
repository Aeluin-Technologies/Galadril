"""Default generator for proposing relation candidates based on schema constraints."""

from __future__ import annotations

import structlog

from eru.schema import GraphSchema
from eru.common.types import CanonicalEntity, RelationCandidate

logger = structlog.get_logger(__name__)


class DefaultRelationCandidateGenerator:
    """Proposes potential relationship edges between a list of canonical entities.

    Filters proposals based on whether the entity labels intersect with allowed
    source and target rules defined in the schema constraints.

    Attributes:
        schema: The GraphSchema containing structural graph constraints.
        allow_self_loops: If True, allows an entity to link to itself.
    """

    def __init__(self, schema: GraphSchema, allow_self_loops: bool = False):
        """Initializes the candidate generator with schema validation rules."""
        self.schema = schema
        self.allow_self_loops = allow_self_loops
        logger.info(
            "relation_candidate_generator_initialized",
            allow_self_loops=allow_self_loops,
            constraints_count=len(schema.constraints),
        )

    def propose(
        self, entities: list[CanonicalEntity]
    ) -> list[RelationCandidate]:
        """Evaluates pairs of entities and proposes valid potential relationships.

        Args:
            entities: A list of discovered canonical entities.

        Returns:
            A list of valid relation candidate links.
        """
        entities_count = len(entities)
        max_possible_combinations = (
            entities_count * entities_count
            if self.allow_self_loops
            else entities_count * (entities_count - 1)
            if entities_count > 1
            else 0
        )

        log = logger.bind(
            entities_count=entities_count,
            max_possible_combinations=max_possible_combinations,
        )
        log.info("proposing_relation_candidates_started")

        candidates = []

        for source_index, source in enumerate(entities):
            source_id = f"ent_{source_index}"
            source_labels = set(source.labels)

            for target_index, target in enumerate(entities):
                target_id = f"ent_{target_index}"

                if not self.allow_self_loops and source_id == target_id:
                    continue

                target_labels = set(target.labels)

                if self._is_possible(source_labels, target_labels):
                    candidates.append(
                        RelationCandidate(
                            source_id=source_id, target_id=target_id
                        )
                    )

        log.info(
            "proposing_relation_candidates_completed",
            proposed_candidates_count=len(candidates),
            filtered_out_combinations=max_possible_combinations
            - len(candidates),
        )
        return candidates

    def _is_possible(
        self, source_labels: set[str], target_labels: set[str]
    ) -> bool:
        """Checks if a pair of labels is permitted by any schema constraint.

        Args:
            source_labels: Set of labels assigned to the source entity.
            target_labels: Set of labels assigned to the target entity.

        Returns:
            True if at least one constraint allows this directional linkage.
        """
        for constraint in self.schema.constraints:
            has_valid_source = bool(source_labels & constraint.allowed_source)
            has_valid_target = bool(target_labels & constraint.allowed_target)

            if has_valid_source and has_valid_target:
                return True

        logger.debug(
            "combination_rejected_by_schema_constraints",
            source_labels=list(source_labels),
            target_labels=list(target_labels),
        )
        return False
