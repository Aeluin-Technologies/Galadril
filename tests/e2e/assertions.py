"""Polling and cross-service assertion helpers for pipeline E2E tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence


async def eventually[T](
    operation: Callable[[], Awaitable[T | None]],
    *,
    timeout_seconds: float,
    description: str,
) -> T:
    """Returns the first non-None result before a monotonic deadline."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = await operation()
            if result is not None:
                return result
        except Exception as error:
            last_error = error
        await asyncio.sleep(0.5)
    suffix = f"; last error: {last_error}" if last_error is not None else ""
    raise AssertionError(f"Timed out waiting for {description}{suffix}")


def require_mapping(value: object, label: str) -> dict[str, object]:
    """Narrows one decoded JSON value to an object with a useful failure."""
    if not isinstance(value, dict):
        raise AssertionError(f"{label} must be an object, got {type(value)}")
    if not all(isinstance(key, str) for key in value):
        raise AssertionError(f"{label} contains a non-string key")
    return value


def require_sequence(value: object, label: str) -> Sequence[object]:
    """Narrows one decoded JSON value to a non-string sequence."""
    if not isinstance(value, list):
        raise AssertionError(f"{label} must be an array, got {type(value)}")
    return value


def tempo_service_names(trace: Mapping[str, object]) -> set[str]:
    """Collects OTLP service names from one Tempo trace response."""
    names: set[str] = set()
    batches = trace.get("batches") or trace.get("resourceSpans") or []
    if not isinstance(batches, list):
        return names
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        resource = batch.get("resource")
        if not isinstance(resource, dict):
            continue
        attributes = resource.get("attributes")
        if not isinstance(attributes, list):
            continue
        for attribute in attributes:
            if (
                not isinstance(attribute, dict)
                or attribute.get("key") != "service.name"
            ):
                continue
            value = attribute.get("value")
            if not isinstance(value, dict):
                continue
            service_name = value.get("stringValue")
            if isinstance(service_name, str):
                names.add(service_name)
    return names
