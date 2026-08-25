"""Shared tenant and stable-resource identity validation."""

from __future__ import annotations

import re

_TENANT_ID_MAX_LEN = 128
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RESOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def normalize_tenant_id(value: object) -> str:
    """Normalizes the fail-closed tenant key shared by platform stores."""
    if not isinstance(value, str):
        raise ValueError("tenant_id must be a string")
    tenant_id = value.strip()
    if tenant_id.startswith("tenant:"):
        _, tenant_id = tenant_id.split(":", 1)
        tenant_id = tenant_id.strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if len(tenant_id) > _TENANT_ID_MAX_LEN:
        raise ValueError("tenant_id exceeds maximum length")
    if _TENANT_ID_RE.fullmatch(tenant_id) is None:
        raise ValueError("tenant_id contains unsupported characters")
    return tenant_id


def require_same_tenant(expected: object, actual: object) -> str:
    """Returns the normalized tenant only when both scopes are identical."""
    expected_tenant = normalize_tenant_id(expected)
    actual_tenant = normalize_tenant_id(actual)
    if expected_tenant != actual_tenant:
        raise ValueError(
            f"tenant mismatch: expected {expected_tenant}, got {actual_tenant}"
        )
    return expected_tenant


def validate_resource_id(value: str) -> str:
    """Validates a stable, display-name-independent ontology identifier."""
    if _RESOURCE_ID_RE.fullmatch(value) is None:
        raise ValueError(
            "resource_id must contain at least two lowercase dotted segments"
        )
    return value


def validate_branch_name(value: str) -> str:
    """Validates a lightweight tenant-local branch reference name."""
    if _BRANCH_NAME_RE.fullmatch(value) is None or ".." in value:
        raise ValueError("branch name contains unsupported characters")
    return value
