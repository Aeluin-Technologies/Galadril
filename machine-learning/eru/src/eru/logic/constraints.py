"""Indexing and graph topology utilities for verifying validation constraints."""

from __future__ import annotations

from collections import defaultdict

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
    """Detects if a directed graph contains any cycles using Depth-First Search (DFS).

    Args:
        edges: A list of directional relationship links tracking (source, target).

    Returns:
        True if at least one cycle/closed loop is identified, otherwise False.
    """
    if not edges:
        return False

    graph = defaultdict(list)
    for src, dst in edges:
        graph[src].append(dst)

    visited = set()
    stack = set()

    def dfs(node: str) -> bool:
        """Recursive helper to track back-edges using an active recursion stack."""
        if node in stack:
            logger.debug("dfs_cycle_backedge_detected", cyclical_node=node)
            return True
        if node in visited:
            return False

        visited.add(node)
        stack.add(node)

        for child in graph[node]:
            if dfs(child):
                return True

        stack.remove(node)
        return False

    logger.debug(
        "executing_cycle_detection_dfs",
        total_edges=len(edges),
        unique_nodes=len(graph),
    )

    cycle_found = any(dfs(node) for node in graph)

    if cycle_found:
        logger.debug("graph_topology_contains_cycles")
    return cycle_found
