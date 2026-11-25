"""Polling and cross-service assertion helpers for pipeline E2E tests."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence

_SPAN_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9._/-]{0,95}", re.ASCII)


async def eventually[T](
    operation: Callable[[], Awaitable[T | None]],
    *,
    timeout_seconds: float,
    description: str,
    abort_on: tuple[type[Exception], ...] = (),
) -> T:
    """Waits past None and False readiness observations before the deadline."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while (remaining := deadline - time.monotonic()) > 0:
        timeout = asyncio.timeout(remaining)
        try:
            async with timeout:
                result = await operation()
            # Empty pages and zero counts can be completed observations.
            if result is not None and result is not False:
                return result
        except abort_on:
            raise
        except TimeoutError as error:
            if timeout.expired() and last_error is not None:
                break
            last_error = error
            if timeout.expired():
                break
        except Exception as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(0.5, remaining))
    suffix = (
        f"; last error: {str(last_error) or type(last_error).__name__}"
        if last_error is not None
        else ""
    )
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


def complete_tempo_trace(
    trace: dict[str, object] | None, required_services: frozenset[str]
) -> dict[str, object] | None:
    """Waits for all service spans in an asynchronously exported trace."""
    if trace is None or not required_services.issubset(
        tempo_service_names(trace)
    ):
        return None
    return trace


def tempo_error_spans(trace: Mapping[str, object]) -> tuple[str, ...]:
    """Reports span names without releasing messages, attributes or events."""
    names: set[str] = set()
    batches = trace.get("batches") or trace.get("resourceSpans")
    if not isinstance(batches, list):
        return ()
    for batch in batches:
        scopes = batch.get("scopeSpans") if isinstance(batch, dict) else None
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            spans = scope.get("spans") if isinstance(scope, dict) else None
            if not isinstance(spans, list):
                continue
            for span in spans:
                if not isinstance(span, dict):
                    continue
                status, name = span.get("status"), span.get("name")
                if (
                    isinstance(status, dict)
                    and status.get("code") in (2, "STATUS_CODE_ERROR")
                    and isinstance(name, str)
                    and _SPAN_NAME.fullmatch(name)
                ):
                    names.add(name)
    return tuple(sorted(names)[:24])
