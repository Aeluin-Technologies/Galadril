"""Graph validator module enforcing schema and structural rules on extracted edges."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

import structlog

from eru.common.exceptions import LogicValidationError
from eru.logic.constraints import ConstraintIndex, has_cycle
from eru.schema import RelationConstraint

logger = structlog.get_logger(__name__)


class ConstraintValidator:
    """Validates knowledge graph edges against schema typing constraints.

    Enforces source/target entity types, structural uniqueness, and directional
    acyclicity rules defined within the graph schema.

    Attributes:
        index: Index mapping relation types to their constraints.
        get_entities: Callable utility to fetch entities from a graph model.
        get_relations: Callable utility to fetch relations from a graph model.
        entity_id: Name of the identifier field on entities.
        entity_type: Name of the type/category field on entities.
        relation_type: Name of the classification field on relations.
        source: Name of the originating entity identifier field on relations.
        target: Name of the destination entity identifier field on relations.
    """

    def __init__(
        self,
        constraints: list[RelationConstraint],
        get_entities: Callable[[Any], list[Any]],
        get_relations: Callable[[Any], list[Any]],
        entity_id: str = "id",
        entity_type: str = "type",
        relation_type: str = "relation_type",
        source: str = "source_id",
        target: str = "target_id",
    ):
        """Initializes the constraint validator configuration mapping defaults."""
        self.index = ConstraintIndex(constraints)
        self.get_entities = get_entities
        self.get_relations = get_relations
        self.entity_id = entity_id
        self.entity_type = entity_type
        self.relation_type = relation_type
        self.source = source
        self.target = target

        logger.info(
            "constraint_validator_initialized",
            entity_id_field=entity_id,
            entity_type_field=entity_type,
            relation_type_field=relation_type,
        )

    def validate(self, graph: Any) -> Any:
        """Validates all graph edges against node typing and topologic rules.

        Filters out edges that violate matching type constraints, unique limits,
        or break directional loop (acyclic) rules.

        Args:
            graph: The instantiated Pydantic knowledge graph tracking components.

        Returns:
            A reconstructed knowledge graph containing only valid relationships.

        Raises:
            LogicValidationError: If graph structural inspection encounters an error.
        """
        try:
            raw_entities = self.get_entities(graph)
            raw_relations = self.get_relations(graph)

            log = logger.bind(
                incoming_entities_count=len(raw_entities),
                incoming_relations_count=len(raw_relations),
            )
            log.info("graph_structural_validation_started")

            entity_types = {
                getattr(ent, self.entity_id): getattr(ent, self.entity_type)
                for ent in raw_entities
            }

            valid_relations = []
            grouped_by_type = defaultdict(list)

            skipped_invalid_source = 0
            skipped_invalid_target = 0

            for rel in raw_relations:
                rel_type = getattr(rel, self.relation_type)
                constraint = self.index.get(rel_type)

                if constraint is None:
                    valid_relations.append(rel)
                    grouped_by_type[rel_type].append(rel)
                    continue

                source_id = getattr(rel, self.source)
                target_id = getattr(rel, self.target)
                source_type = entity_types.get(source_id)
                target_type = entity_types.get(target_id)

                if source_type not in constraint.allowed_source:
                    skipped_invalid_source += 1
                    logger.debug(
                        "edge_rejected_invalid_source_type",
                        relation_type=rel_type,
                        source_id=source_id,
                        source_type=source_type,
                        allowed_sources=constraint.allowed_source,
                    )
                    continue

                if target_type not in constraint.allowed_target:
                    skipped_invalid_target += 1
                    logger.debug(
                        "edge_rejected_invalid_target_type",
                        relation_type=rel_type,
                        target_id=target_id,
                        target_type=target_type,
                        allowed_targets=constraint.allowed_target,
                    )
                    continue

                valid_relations.append(rel)
                grouped_by_type[rel_type].append(rel)

            log.debug(
                "node_type_constraints_applied",
                passed_count=len(valid_relations),
                skipped_invalid_source=skipped_invalid_source,
                skipped_invalid_target=skipped_invalid_target,
            )

            unique_relations = []
            seen_unique_keys = set()
            skipped_duplicates = 0

            for rel in valid_relations:
                rel_type = getattr(rel, self.relation_type)
                constraint = self.index.get(rel_type)

                if constraint and constraint.unique:
                    source_id = getattr(rel, self.source)
                    key = (rel_type, source_id)

                    if key in seen_unique_keys:
                        skipped_duplicates += 1
                        logger.debug(
                            "edge_rejected_uniqueness_violation",
                            relation_type=rel_type,
                            source_id=source_id,
                        )
                        continue
                    seen_unique_keys.add(key)

                unique_relations.append(rel)

            log.debug(
                "uniqueness_constraints_applied",
                passed_count=len(unique_relations),
                skipped_duplicates=skipped_duplicates,
            )

            final_relations = []
            unique_grouped = defaultdict(list)
            for rel in unique_relations:
                unique_grouped[getattr(rel, self.relation_type)].append(rel)

            skipped_cyclical_groups = 0

            for rel_type, relations in unique_grouped.items():
                constraint = self.index.get(rel_type)

                if not constraint or not constraint.acyclic:
                    final_relations.extend(relations)
                    continue

                edges = [
                    (getattr(r, self.source), getattr(r, self.target))
                    for r in relations
                ]

                if has_cycle(edges):
                    skipped_cyclical_groups += len(relations)
                    log.warning(
                        "relation_topology_contains_cycle_dropping_group",
                        relation_type=rel_type,
                        dropped_edges_count=len(relations),
                    )
                    continue

                final_relations.extend(relations)

            log.info(
                "graph_structural_validation_completed",
                final_valid_relations_count=len(final_relations),
                total_dropped_edges=len(raw_relations) - len(final_relations),
                skipped_cyclical_edges=skipped_cyclical_groups,
            )
            return self._rebuild(graph, final_relations)

        except Exception as e:
            logger.exception("graph_validation_pipeline_crashed")
            raise LogicValidationError(str(e)) from e

    def _rebuild(self, graph: Any, relations: list[Any]) -> Any:
        """Dumps and updates the Pydantic graph model state with valid edges."""
        logger.debug(
            "rebuilding_pydantic_graph_state",
            final_relations_count=len(relations),
        )
        data = graph.model_dump()
        data["relations"] = [r.model_dump() for r in relations]
        return type(graph).model_validate(data)
