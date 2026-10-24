"""Compiles reviewed schemas without allowing deployment-time network resolution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import JsonValue


class ParameterValidator(Protocol):
    def validate(self, instance: object) -> None: ...


def _local_references(value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise ValueError("Plugin schemas require local references")
            _local_references(item)
    elif isinstance(value, list):
        for item in value:
            _local_references(item)


def compile_parameters(schema: dict[str, JsonValue]) -> ParameterValidator:
    """Validates once before any handler, toolset, or service is constructed."""
    _local_references(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("Invalid plugin parameter schema") from None
    return cast(ParameterValidator, Draft202012Validator(schema))


def validate_parameters(
    validator: ParameterValidator, parameters: Mapping[str, JsonValue]
) -> None:
    """Keeps schema failures from including private parameter values in logs."""
    try:
        validator.validate(parameters)
    except (ValidationError, RecursionError):
        raise ValueError(
            "Plugin parameters do not match the declared schema"
        ) from None
