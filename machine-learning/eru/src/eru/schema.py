"""Module defining schemas and validation constraints for graph-based models."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field
from eru.common.types import RelationDef


class RelationConstraint(BaseModel):
    """Defines validation rules and constraints for a specific relation type.

    Attributes:
        relation: The name of the relation being constrained.
        allowed_source: Permitted types for the source entity.
        allowed_target: Permitted types for the target entity.
        unique: If True, enforces a unique pair connection. Defaults to False.
        acyclic: If True, prevents cycles within this relation. Defaults to False.
        required: If True, the relation must exist. Defaults to False.
    """

    relation: str
    allowed_source: set[str]
    allowed_target: set[str]
    unique: bool = False
    acyclic: bool = False
    required: bool = False


class GraphSchema(BaseModel):
    """Schema representing the structural definition of a graph model.

    Attributes:
        entity_model: The Pydantic model type used for entities.
        relation_model: The Pydantic model type used for relations.
        graph_model: The Pydantic model type used for the overall graph.
        relation_defs: Definitions of the relations in the graph.
        constraints: Multi-entity validation constraints.
        implicit_entity_types: Entity types inferred rather than explicitly declared.
        metadata: Arbitrary key-value metadata for the schema.
    """

    entity_model: type[BaseModel]
    relation_model: type[BaseModel]
    graph_model: type[BaseModel]
    relation_defs: list[RelationDef] = Field(default_factory=list)
    constraints: list[RelationConstraint] = Field(default_factory=list)
    implicit_entity_types: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
