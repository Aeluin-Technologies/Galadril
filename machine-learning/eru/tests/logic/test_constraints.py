"""Tests for graph topology verification and edge validation constraints."""

from __future__ import annotations

import pytest
from eru.common.exceptions import LogicValidationError
from eru.logic.constraints import has_cycle
from eru.logic.simple import ConstraintValidator
from eru.schema import RelationConstraint
from pydantic import BaseModel


class SimpleEntity(BaseModel):
    id: str
    type: str


class SimpleRelation(BaseModel):
    source_id: str
    target_id: str
    relation_type: str


class SimpleGraph(BaseModel):
    entities: list[SimpleEntity]
    relations: list[SimpleRelation]


def test_has_cycle_detects_loops() -> None:
    """Validates directed acyclic path loops validation."""
    assert has_cycle([("A", "B"), ("B", "C"), ("C", "A")]) is True
    assert has_cycle([("A", "B"), ("B", "C")]) is False


def test_constraint_validator_filters_illegal_types_and_duplicates() -> None:
    """Ensures graph components violating schema limits are discarded safely."""
    constraints = [
        RelationConstraint(
            relation="leads",
            allowed_source={"PERSON"},
            allowed_target={"ORG"},
            unique=True,
            acyclic=True,
        )
    ]
    validator = ConstraintValidator(
        constraints=constraints,
        get_entities=lambda g: g.entities,
        get_relations=lambda g: g.relations,
    )

    graph = SimpleGraph(
        entities=[
            SimpleEntity(id="e1", type="PERSON"),
            SimpleEntity(id="e2", type="INVALID_TYPE"),
            SimpleEntity(id="e3", type="ORG"),
        ],
        relations=[
            SimpleRelation(
                source_id="e1", target_id="e2", relation_type="leads"
            ),
            SimpleRelation(
                source_id="e1", target_id="e3", relation_type="leads"
            ),
            SimpleRelation(
                source_id="e1", target_id="e3", relation_type="leads"
            ),
        ],
    )

    clean_graph = validator.validate(graph)
    assert len(clean_graph.relations) == 1
    assert clean_graph.relations[0].target_id == "e3"


def test_constraint_validator_crashes_raise_logic_validation_error() -> None:
    """Ensures field errors map safely to runtime LogicValidationError exceptions."""
    validator = ConstraintValidator([], lambda g: [], lambda g: [])
    with pytest.raises(LogicValidationError):
        validator.validate(None)
