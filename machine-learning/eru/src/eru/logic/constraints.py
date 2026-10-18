"""Indexing and graph topology utilities for verifying validation constraints."""

from __future__ import annotations

from collections import deque

import structlog

from eru.schema import RelationConstraint

logger = structlog.get_logger(__name__)


class ConstraintIndex:
    """Provides fast O(1) lookup index for relation schema constraints.

    Attributes:
        by_relation: Map linking a relation's name string to its constraint definitions.
    """

    def __init__(self, constraints: list[RelationConstraint]):
        """Initializes the lookup index from a raw list of schema constraints."""
        self.by_relation = {x.relation: x for x in constraints}
        logger.info(
            "constraint_index_initialized",
            indexed_relations_count=len(self.by_relation),
        )

    def get(self, relation: str) -> RelationConstraint | None:
        """Retrieves the schema constraint definition for a specific relation name.

        Args:
            relation: The relation identifier name string.

        Returns:
            The matching RelationConstraint definition, or None if unconstrained.
        """
        constraint = self.by_relation.get(relation)
        if constraint is None:
            logger.debug("constraint_lookup_miss", relation_type=relation)
        return constraint


def has_cycle(edges: list[tuple[str, str]]) -> bool:
    """Detect cycles without recursion or mutating adjacency during traversal.

    Args:
        edges: A list of directional relationship links tracking (source, target).

    Returns:
        True if at least one cycle/closed loop is identified, otherwise False.
    """
    if not edges:
        return False

    graph: dict[str, list[str]] = {}
    indegree: dict[str, int] = {}
    for src, dst in edges:
        graph.setdefault(src, []).append(dst)
        indegree.setdefault(src, 0)
        indegree[dst] = indegree.get(dst, 0) + 1
    ready = deque(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        node = ready.popleft()
        visited += 1
        for child in graph.get(node, ()):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    cyclic = visited != len(indegree)
    logger.debug(
        "graph_cycle_detection_completed",
        edge_count=len(edges),
        node_count=len(indegree),
        cyclic=cyclic,
    )
    return cyclic
